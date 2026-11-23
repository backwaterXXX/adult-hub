"""Client API Authentication and TV Pairing service layer."""

from __future__ import annotations

import re
import secrets
import time
from typing import Any, NoReturn

from common.client_auth.crypto import (
    TokenValidationError,
    dummy_verify,
    generate_pairing_code,
    generate_pairing_secret,
    generate_token,
    hash_password,
    hash_token,
    normalize_pairing_code,
    verify_password,
)
from common.client_auth.rate_limit import check_and_increment_rate_limit


class AuthError(Exception):
    """Base exception for client authentication errors."""
    status_code: int = 400


class AuthenticationFailed(AuthError):
    status_code: int = 401


class RateLimitExceeded(AuthError):
    status_code: int = 429


class BoundedValidationError(AuthError):
    status_code: int = 400


class PairingNotFoundOrExpired(AuthError):
    status_code: int = 404


class PairingAlreadyExchanged(AuthError):
    status_code: int = 401


# Bounds
MIN_USERNAME_LEN = 3
MAX_USERNAME_LEN = 64
MIN_PASSWORD_LEN = 8
MAX_PASSWORD_LEN = 128
MAX_DEVICE_ID_LEN = 128
MAX_DEVICE_NAME_LEN = 128
MAX_PAIRING_POLLS = 150

ACCESS_TOKEN_LIFETIME_SECONDS = 900  # 15 minutes
REFRESH_TOKEN_LIFETIME_SECONDS = 30 * 86400  # 30 days
PAIRING_CODE_LIFETIME_SECONDS = 300  # 5 minutes
PAIRING_POLL_INTERVAL_SECONDS = 2

OWNER_BOOTSTRAP_LOCK_ID = 0xAD01B007


def _validate_bounds(value: str, name: str, min_len: int, max_len: int) -> None:
    if not isinstance(value, str):
        raise BoundedValidationError(f"{name} must be a string")
    if "\x00" in value:
        raise BoundedValidationError(f"{name} must not contain null characters")
    if len(value) < min_len or len(value) > max_len:
        raise BoundedValidationError(
            f"{name} length must be between {min_len} and {max_len} characters"
        )


def _validate_optional_string(value: Any, name: str, max_len: int) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise BoundedValidationError(f"{name} must be a string")
    if "\x00" in value:
        raise BoundedValidationError(f"{name} must not contain null characters")
    if len(value) > max_len:
        raise BoundedValidationError(f"{name} length exceeds maximum of {max_len} characters")
    return value


def _safe_hash_token(token: str, name: str = "Token") -> str:
    """Validate untrusted token format as bounded ASCII and return its SHA-256 digest.

    Raises BoundedValidationError (4xx) on non-ASCII or oversized inputs,
    preventing unhandled 500 errors.
    """
    try:
        return hash_token(token)
    except (TokenValidationError, TypeError) as exc:
        raise BoundedValidationError(f"{name} is invalid: {exc}") from exc


def bootstrap_owner(conn, username: str, password: str) -> dict[str, Any]:
    """Bootstrap the initial owner account via local CLI.

    Serialized via PostgreSQL advisory lock and guarded by single-owner check.
    Succeeds only if no admin/owner user exists.
    Passwords are never logged or stored in plain text.
    """
    # Serialize concurrent bootstrap attempts
    conn.execute("SELECT pg_advisory_xact_lock(%s)", (OWNER_BOOTSTRAP_LOCK_ID,))

    # Check if an admin already exists
    existing = conn.execute("SELECT 1 FROM client_users WHERE is_admin = true").fetchone()
    if existing:
        raise ValueError("An owner account already exists. Bootstrap can only be run once.")

    username = username.strip()
    _validate_bounds(username, "Username", MIN_USERNAME_LEN, MAX_USERNAME_LEN)
    _validate_bounds(password, "Password", MIN_PASSWORD_LEN, MAX_PASSWORD_LEN)

    if not re.match(r"^[a-zA-Z0-9_\-\.]+$", username):
        raise BoundedValidationError("Username may only contain letters, numbers, hyphens, and underscores")

    pwd_hash = hash_password(password)
    user_id = f"usr_{secrets.token_hex(16)}"

    cursor = conn.execute(
        "INSERT INTO client_users (id, username, password_hash, is_admin, is_active) "
        "VALUES (%s, %s, %s, true, true) "
        "RETURNING id, username",
        (user_id, username, pwd_hash),
    )
    row = cursor.fetchone()
    conn.commit()
    if not row:
        raise ValueError("Failed to create owner account.")
    return {"id": row[0], "username": row[1]}


def authenticate_by_password(
    conn,
    username: str,
    password: str,
    device_name: str,
    device_id: str,
    client_ip: str,
) -> dict[str, Any]:
    """Authenticate a client user by username and password.

    Enforces scrypt verification, anti-enumeration dummy timing,
    multi-dimensional rate limiting per-IP AND per-account (committed even on failure),
    and returns short-lived access and refresh tokens with zero is_admin exposure.
    """
    _validate_bounds(username, "Username", 1, MAX_USERNAME_LEN)
    _validate_bounds(password, "Password", 1, MAX_PASSWORD_LEN)
    device_name = _validate_optional_string(device_name, "device_name", MAX_DEVICE_NAME_LEN)
    device_id = _validate_optional_string(device_id, "device_id", MAX_DEVICE_ID_LEN)
    if not device_id:
        device_id = "default-device"

    # Multi-dimensional rate limiting: per-IP (max 10/300s) AND per-account (max 5/300s)
    # Both rate counters commit atomically to DB regardless of outcome
    rate_key_ip = f"login_ip:{client_ip}"
    if not check_and_increment_rate_limit(conn, rate_key_ip, max_attempts=10, window_seconds=300):
        raise RateLimitExceeded("Too many login attempts. Please try again later.")

    rate_key_user = f"login_user:{username.strip().lower()}"
    if not check_and_increment_rate_limit(conn, rate_key_user, max_attempts=5, window_seconds=300):
        raise RateLimitExceeded("Too many login attempts for this account. Please try again later.")

    cursor = conn.execute(
        "SELECT id, username, password_hash, is_active FROM client_users WHERE lower(username) = lower(%s)",
        (username.strip(),),
    )
    user_row = cursor.fetchone()

    # Kiro (7): Set SAME denial for disabled / non-existing accounts
    if not user_row:
        dummy_verify(password)
        raise AuthenticationFailed("Invalid username or password")

    user_id, canonical_username, pwd_hash, is_active = user_row
    if not is_active:
        dummy_verify(password)
        raise AuthenticationFailed("Invalid username or password")

    if not verify_password(password, pwd_hash):
        raise AuthenticationFailed("Invalid username or password")

    # Issue tokens
    access_token = generate_token("ah_tok_")
    refresh_token = generate_token("ah_ref_")

    access_hash = hash_token(access_token)
    refresh_hash = hash_token(refresh_token)

    now_epoch = time.time()
    access_expires_epoch = now_epoch + ACCESS_TOKEN_LIFETIME_SECONDS
    refresh_expires_epoch = now_epoch + REFRESH_TOKEN_LIFETIME_SECONDS

    session_id = f"sess_{secrets.token_hex(16)}"
    conn.execute(
        "INSERT INTO client_sessions ("
        "id, user_id, device_id, device_name, access_token_hash, refresh_token_hash, "
        "access_expires_at, refresh_expires_at"
        ") VALUES (%s, %s, %s, %s, %s, %s, to_timestamp(%s), to_timestamp(%s))",
        (
            session_id,
            user_id,
            device_id,
            device_name,
            access_hash,
            refresh_hash,
            access_expires_epoch,
            refresh_expires_epoch,
        ),
    )
    conn.commit()

    return {
        "access_token": access_token,
        "token_type": "Bearer",
        "expires_in": ACCESS_TOKEN_LIFETIME_SECONDS,
        "refresh_token": refresh_token,
        "user": {
            "id": user_id,
            "username": canonical_username,
        },
    }


def _fetch_spent_token_row(conn, token_hash: str):
    """Return the spent-token row (session_id) for a refresh token hash, or None."""
    spent_cursor = conn.execute(
        "SELECT session_id FROM client_session_spent_tokens WHERE token_hash = %s",
        (token_hash,),
    )
    return spent_cursor.fetchone()


def _find_replayed_session_row(conn, incoming_hash: str):
    """Look up an incoming refresh token hash among spent tokens and previous refresh hashes."""
    spent_row = _fetch_spent_token_row(conn, incoming_hash)
    if not spent_row:
        prev_cursor = conn.execute(
            "SELECT id FROM client_sessions WHERE previous_refresh_token_hash = %s",
            (incoming_hash,),
        )
        spent_row = prev_cursor.fetchone()
    return spent_row


def _revoke_replayed_session(conn, session_id) -> NoReturn:
    """Revoke a session whose refresh token was replayed, commit immediately, and reject the request."""
    conn.execute(
        "UPDATE client_sessions SET revoked_at = CURRENT_TIMESTAMP WHERE id = %s",
        (session_id,),
    )
    conn.commit()
    raise AuthenticationFailed("Refresh token replayed. Session has been revoked.")


def _lock_refresh_session_row(conn, incoming_hash: str):
    """Lock the session row matching the refresh token hash, or reject the token."""
    # Match both active refresh_token_hash and previous_refresh_token_hash under lock
    # so concurrent rotations wait on the exact session row and deterministically trigger replay revocation!
    cursor = conn.execute(
        "SELECT s.id, s.user_id, s.device_id, s.device_name, "
        "(s.refresh_expires_at <= CURRENT_TIMESTAMP) AS is_expired, s.revoked_at, "
        "s.refresh_token_hash, s.previous_refresh_token_hash, u.username, u.is_active "
        "FROM client_sessions s "
        "JOIN client_users u ON s.user_id = u.id "
        "WHERE s.refresh_token_hash = %s OR s.previous_refresh_token_hash = %s "
        "FOR UPDATE OF s",
        (incoming_hash, incoming_hash),
    )
    row = cursor.fetchone()
    if row:
        return row
    # Check spent tokens table again in case token was rotated multiple times
    spent_row = _fetch_spent_token_row(conn, incoming_hash)
    if spent_row:
        _revoke_replayed_session(conn, spent_row[0])
    raise AuthenticationFailed("Invalid refresh token")


def refresh_session_token(conn, refresh_token: str) -> dict[str, Any]:
    """Refresh an access token using single-use rotation with row locking and persistent replay detection.

    Enforces RFC 6819 strict rotation without insecure grace windows.
    Replay revocation is committed immediately to the database so that subsequent
    requests cannot use any token from the compromised family.
    Concurrent rotation attempts deterministically detect replay and revoke the session family.
    """
    if not isinstance(refresh_token, str) or not refresh_token:
        raise BoundedValidationError("refresh_token is required")

    incoming_hash = _safe_hash_token(refresh_token, name="refresh_token")

    # 1. Persistent Replay Detection:
    # Check spent tokens table AND previous_refresh_token_hash on sessions
    spent_row = _find_replayed_session_row(conn, incoming_hash)
    if spent_row:
        # Compromised family: immediately revoke session in DB and commit!
        _revoke_replayed_session(conn, spent_row[0])

    # 2. Active Token Lookup with Row Locking (FOR UPDATE)
    row = _lock_refresh_session_row(conn, incoming_hash)

    (
        session_id,
        user_id,
        device_id,
        device_name,
        is_expired,
        revoked_at,
        current_refresh_hash,
        previous_refresh_hash,
        username,
        is_active,
    ) = row

    if revoked_at is not None:
        raise AuthenticationFailed("Session is revoked")

    # If incoming hash is not current_refresh_hash (e.g. it equals previous_refresh_hash,
    # or another concurrent transaction rotated it while this transaction waited for the row lock):
    if current_refresh_hash != incoming_hash:
        _revoke_replayed_session(conn, session_id)

    if not is_active:
        raise AuthenticationFailed("User account is disabled")

    # Check expiry
    if is_expired:
        raise AuthenticationFailed("Refresh token has expired")

    # Rotate tokens transactionally with old hash condition
    new_access_token = generate_token("ah_tok_")
    new_refresh_token = generate_token("ah_ref_")

    new_access_hash = hash_token(new_access_token)
    new_refresh_hash = hash_token(new_refresh_token)

    now_epoch = time.time()
    new_access_expires_epoch = now_epoch + ACCESS_TOKEN_LIFETIME_SECONDS
    new_refresh_expires_epoch = now_epoch + REFRESH_TOKEN_LIFETIME_SECONDS

    # Atomically rotate, requiring that refresh_token_hash still matches incoming_hash
    update_cur = conn.execute(
        "UPDATE client_sessions SET "
        "previous_refresh_token_hash = refresh_token_hash, "
        "refresh_token_hash = %s, "
        "access_token_hash = %s, "
        "access_expires_at = to_timestamp(%s), "
        "refresh_expires_at = to_timestamp(%s), "
        "updated_at = CURRENT_TIMESTAMP "
        "WHERE id = %s AND refresh_token_hash = %s",
        (
            new_refresh_hash,
            new_access_hash,
            new_access_expires_epoch,
            new_refresh_expires_epoch,
            session_id,
            incoming_hash,
        ),
    )
    if update_cur.rowcount == 0:
        # Concurrent rotation occurred! Revoke session family and deny
        _revoke_replayed_session(conn, session_id)

    # Record spent token in persistent spent token table
    conn.execute(
        "INSERT INTO client_session_spent_tokens (session_id, token_hash) "
        "VALUES (%s, %s) ON CONFLICT (token_hash) DO NOTHING",
        (session_id, incoming_hash),
    )
    conn.commit()

    return {
        "access_token": new_access_token,
        "token_type": "Bearer",
        "expires_in": ACCESS_TOKEN_LIFETIME_SECONDS,
        "refresh_token": new_refresh_token,
        "user": {
            "id": user_id,
            "username": username,
        },
    }


def revoke_session_token(conn, refresh_token: str) -> bool:
    """Revoke a session given its current or previous refresh token."""
    if not isinstance(refresh_token, str) or not refresh_token:
        return False
    try:
        token_hash = _safe_hash_token(refresh_token, name="refresh_token")
    except BoundedValidationError:
        return False

    # Check active, previous, or spent
    cursor = conn.execute(
        "SELECT session_id FROM client_session_spent_tokens WHERE token_hash = %s",
        (token_hash,),
    )
    spent = cursor.fetchone()
    if spent:
        conn.execute(
            "UPDATE client_sessions SET revoked_at = CURRENT_TIMESTAMP WHERE id = %s",
            (spent[0],),
        )
        conn.commit()
        return True

    conn.execute(
        "UPDATE client_sessions SET revoked_at = CURRENT_TIMESTAMP "
        "WHERE (refresh_token_hash = %s OR previous_refresh_token_hash = %s) AND revoked_at IS NULL",
        (token_hash, token_hash),
    )
    conn.commit()
    return True


def initiate_pairing(
    conn,
    device_name: str,
    device_id: str,
    client_ip: str,
) -> dict[str, Any]:
    """Initiate a 10-foot TV pairing session with live-unique codes."""
    device_name = _validate_optional_string(device_name, "device_name", MAX_DEVICE_NAME_LEN)
    device_id = _validate_optional_string(device_id, "device_id", MAX_DEVICE_ID_LEN)

    # Rate limit: max 5 init requests per 60s window per client IP
    rate_key = f"pair_init:{client_ip}"
    if not check_and_increment_rate_limit(conn, rate_key, max_attempts=5, window_seconds=60):
        raise RateLimitExceeded("Too many pairing attempts. Please try again shortly.")

    # Expire past pending/approved rows before generating codes
    conn.execute(
        "UPDATE client_pairing_sessions "
        "SET status = 'expired' "
        "WHERE status IN ('pending', 'approved') AND expires_at <= CURRENT_TIMESTAMP"
    )
    conn.commit()

    secret = generate_pairing_secret()
    secret_hash = hash_token(secret)
    session_id = f"pair_{secrets.token_hex(16)}"
    expires_epoch = time.time() + PAIRING_CODE_LIFETIME_SECONDS

    # Generate live-unique code (retrying if collision on pending/approved code)
    pairing_code = ""
    for _ in range(5):
        candidate_code = generate_pairing_code()
        norm = normalize_pairing_code(candidate_code)
        try:
            conn.execute("SAVEPOINT pair_candidate")
        except Exception:
            pass
        try:
            cursor = conn.execute(
                "INSERT INTO client_pairing_sessions ("
                "id, pairing_code, normalized_code, secret_hash, device_name, device_id, status, "
                "poll_attempts, expires_at, client_ip"
                ") VALUES (%s, %s, %s, %s, %s, %s, 'pending', 0, to_timestamp(%s), %s) "
                "ON CONFLICT (normalized_code) WHERE status IN ('pending', 'approved') DO NOTHING "
                "RETURNING id",
                (
                    session_id,
                    candidate_code,
                    norm,
                    secret_hash,
                    device_name,
                    device_id,
                    expires_epoch,
                    client_ip,
                ),
            )
            row = cursor.fetchone() if cursor else None
            if row:
                try:
                    conn.execute("RELEASE SAVEPOINT pair_candidate")
                except Exception:
                    pass
                conn.commit()
                pairing_code = candidate_code
                break
            else:
                try:
                    conn.execute("ROLLBACK TO SAVEPOINT pair_candidate")
                except Exception:
                    pass
        except Exception:
            try:
                conn.execute("ROLLBACK TO SAVEPOINT pair_candidate")
            except Exception:
                pass
            continue

    if not pairing_code:
        raise AuthError("Unable to generate unique pairing code. Please try again.")

    return {
        "pairing_code": pairing_code,
        "secret": secret,
        "expires_in": PAIRING_CODE_LIFETIME_SECONDS,
        "poll_interval_seconds": PAIRING_POLL_INTERVAL_SECONDS,
    }


def approve_pairing(conn, pairing_code: str, approver_user_id: str | None = None) -> dict[str, Any]:
    """Approve a pending TV pairing code via Admin V2.

    Strictly performed out-of-band by Admin V2. Client tokens cannot approve codes.
    Requires an active owner/admin user account.
    """
    _validate_bounds(pairing_code, "pairing_code", 1, 32)
    if approver_user_id is not None:
        _validate_bounds(str(approver_user_id), "user_id", 1, 64)

    normalized = normalize_pairing_code(pairing_code)
    if not normalized or len(normalized) != 6:
        raise PairingNotFoundOrExpired("Invalid pairing code")

    # Validate approver user: must be an active admin / owner
    if not approver_user_id:
        admin_row = conn.execute(
            "SELECT id FROM client_users WHERE is_admin = true AND is_active = true LIMIT 1"
        ).fetchone()
        if not admin_row:
            raise ValueError("No active owner account exists to associate with pairing.")
        effective_user_id = admin_row[0]
    else:
        user_row = conn.execute(
            "SELECT id FROM client_users WHERE id = %s AND is_admin = true AND is_active = true",
            (approver_user_id,),
        ).fetchone()
        if not user_row:
            raise AuthenticationFailed("Approver must be an active administrator.")
        effective_user_id = user_row[0]

    # Find and lock active pending pairing session by indexed normalized_code
    cursor = conn.execute(
        "SELECT id, device_name, expires_at "
        "FROM client_pairing_sessions "
        "WHERE normalized_code = %s "
        "AND status = 'pending' "
        "AND expires_at > CURRENT_TIMESTAMP "
        "FOR UPDATE",
        (normalized,),
    )
    row = cursor.fetchone()
    if not row:
        raise PairingNotFoundOrExpired("Pairing code not found or expired")

    session_id, device_name, _ = row
    conn.execute(
        "UPDATE client_pairing_sessions SET "
        "status = 'approved', "
        "user_id = %s, "
        "approved_at = CURRENT_TIMESTAMP "
        "WHERE id = %s AND status = 'pending'",
        (effective_user_id, session_id),
    )
    conn.commit()
    return {"approved": True, "device_name": device_name}


def _lock_pairing_row(conn, secret_hash: str):
    """Lock the pairing session row for a secret hash. Unknown secrets cause no database writes."""
    # Lock row for status check FIRST before performing any rate limit writes
    # Prevents database writes/bloat for bogus or unknown pairing secrets
    cursor = conn.execute(
        "SELECT p.id, p.device_name, p.device_id, p.user_id, p.status, p.poll_attempts, "
        "(p.expires_at <= CURRENT_TIMESTAMP) AS is_expired, "
        "GREATEST(0, FLOOR(EXTRACT(EPOCH FROM (p.expires_at - CURRENT_TIMESTAMP))))::BIGINT AS remaining_seconds, "
        "u.username, u.is_active "
        "FROM client_pairing_sessions p "
        "LEFT JOIN client_users u ON p.user_id = u.id "
        "WHERE p.secret_hash = %s "
        "FOR UPDATE OF p",
        (secret_hash,),
    )
    row = cursor.fetchone()
    if not row:
        # Unknown pairing secret: strictly zero writes to database (prevents table bloat & denial of service)
        raise PairingNotFoundOrExpired("Pairing session not found")
    return row


def _mark_pairing_expired(conn, session_id) -> None:
    """Set a pairing session to expired and commit immediately."""
    conn.execute(
        "UPDATE client_pairing_sessions SET status = 'expired' WHERE id = %s",
        (session_id,),
    )
    conn.commit()


def _exchange_approved_pairing(
    conn,
    session_id,
    user_id,
    device_id,
    device_name,
    is_active,
    username,
) -> dict[str, Any]:
    """Atomically exchange an approved pairing for a new client session and return its tokens."""
    # Atomic claim: one-time exchange
    update_cur = conn.execute(
        "UPDATE client_pairing_sessions SET "
        "status = 'exchanged', "
        "exchanged_at = CURRENT_TIMESTAMP "
        "WHERE id = %s AND status = 'approved' AND expires_at > CURRENT_TIMESTAMP",
        (session_id,),
    )
    if update_cur.rowcount == 0:
        raise PairingAlreadyExchanged("Pairing session has already been exchanged")

    # Validate that user_id is non-null and user is active
    if not user_id or not is_active:
        conn.execute(
            "UPDATE client_pairing_sessions SET status = 'revoked' WHERE id = %s",
            (session_id,),
        )
        conn.commit()
        raise AuthenticationFailed("User account associated with pairing is inactive or invalid")

    access_token = generate_token("ah_tok_")
    refresh_token = generate_token("ah_ref_")

    access_hash = hash_token(access_token)
    refresh_hash = hash_token(refresh_token)

    now_epoch = time.time()
    access_expires_epoch = now_epoch + ACCESS_TOKEN_LIFETIME_SECONDS
    refresh_expires_epoch = now_epoch + REFRESH_TOKEN_LIFETIME_SECONDS

    client_session_id = f"sess_{secrets.token_hex(16)}"
    conn.execute(
        "INSERT INTO client_sessions ("
        "id, user_id, device_id, device_name, access_token_hash, refresh_token_hash, "
        "access_expires_at, refresh_expires_at"
        ") VALUES (%s, %s, %s, %s, %s, %s, to_timestamp(%s), to_timestamp(%s))",
        (
            client_session_id,
            user_id,
            device_id or "tv-paired",
            device_name or "Android TV",
            access_hash,
            refresh_hash,
            access_expires_epoch,
            refresh_expires_epoch,
        ),
    )
    conn.commit()

    return {
        "status": "approved",
        "access_token": access_token,
        "token_type": "Bearer",
        "expires_in": ACCESS_TOKEN_LIFETIME_SECONDS,
        "refresh_token": refresh_token,
        "user": {
            "id": user_id,
            "username": username or "owner",
        },
    }


def poll_pairing_status(conn, secret: str, client_ip: str) -> dict[str, Any]:
    """Poll TV pairing status using private secret.

    Atomic claim and one-time token issuance.
    Strictly prevents database writes for unknown pairing secrets.
    Poll count increments and rate limits for legitimate sessions are committed immediately to DB.
    """
    if not isinstance(secret, str) or not secret:
        raise BoundedValidationError("Pairing secret is required")

    secret_hash = _safe_hash_token(secret, name="secret")

    row = _lock_pairing_row(conn, secret_hash)

    (
        session_id,
        device_name,
        device_id,
        user_id,
        status,
        poll_attempts,
        is_expired,
        remaining_seconds,
        username,
        is_active,
    ) = row

    # 2. Rate limiting for valid sessions: max 2 requests per 1s per session (committed to DB)
    rate_key = f"pair_status:{secret_hash}"
    if not check_and_increment_rate_limit(conn, rate_key, max_attempts=2, window_seconds=1):
        raise RateLimitExceeded("Polling too rapidly. Respect poll_interval_seconds.")

    # Increment poll attempts and commit immediately
    new_attempts = poll_attempts + 1
    conn.execute(
        "UPDATE client_pairing_sessions SET poll_attempts = %s WHERE id = %s",
        (new_attempts, session_id),
    )
    conn.commit()

    if new_attempts > MAX_PAIRING_POLLS:
        _mark_pairing_expired(conn, session_id)
        raise RateLimitExceeded("Maximum polling attempts exceeded for this pairing session")

    # Check expiry
    if is_expired:
        _mark_pairing_expired(conn, session_id)
        raise PairingNotFoundOrExpired("Pairing session has expired")

    if status == "pending":
        return {
            "status": "pending",
            "expires_in": remaining_seconds,
        }

    if status == "approved":
        return _exchange_approved_pairing(
            conn, session_id, user_id, device_id, device_name, is_active, username,
        )

    if status == "exchanged":
        raise PairingAlreadyExchanged("Pairing session has already been exchanged")

    raise PairingNotFoundOrExpired("Pairing session is invalid")


def verify_access_token(conn, access_token: str) -> dict[str, Any] | None:
    """Validate bearer access token against client_sessions and client_users."""
    if not isinstance(access_token, str) or not access_token:
        return None

    try:
        token_hash = _safe_hash_token(access_token, name="access_token")
    except BoundedValidationError:
        return None

    cursor = conn.execute(
        "SELECT s.id, s.user_id, s.device_id, s.device_name, u.username, u.is_admin, u.is_active "
        "FROM client_sessions s "
        "JOIN client_users u ON s.user_id = u.id "
        "WHERE s.access_token_hash = %s "
        "AND s.revoked_at IS NULL "
        "AND s.access_expires_at > CURRENT_TIMESTAMP "
        "AND u.is_active = true",
        (token_hash,),
    )
    row = cursor.fetchone()
    if not row:
        return None
    return {
        "session_id": row[0],
        "user_id": row[1],
        "device_id": row[2],
        "device_name": row[3],
        "username": row[4],
        "is_admin": row[5],
        "is_active": row[6],
    }


def prune_retired_auth_data(conn) -> dict[str, int]:
    """Prune expired rate limit counters, expired pairing sessions, and retired session records."""
    now_epoch = int(time.time())
    one_hour_ago = now_epoch - 3600

    # Rate limits older than 1 hour
    cur = conn.execute(
        "DELETE FROM client_auth_rate_limits WHERE window_epoch < %s",
        (one_hour_ago,),
    )
    pruned_rates = cur.rowcount

    # Pairing sessions expired or exchanged > 2 days ago
    cur = conn.execute(
        "DELETE FROM client_pairing_sessions WHERE expires_at < CURRENT_TIMESTAMP - INTERVAL '2 days'"
    )
    pruned_pairs = cur.rowcount

    # Revoked sessions older than 30 days
    cur = conn.execute(
        "DELETE FROM client_sessions WHERE (revoked_at IS NOT NULL AND revoked_at < CURRENT_TIMESTAMP - INTERVAL '30 days') "
        "OR (refresh_expires_at < CURRENT_TIMESTAMP - INTERVAL '30 days')"
    )
    pruned_sessions = cur.rowcount

    conn.commit()
    return {
        "pruned_rate_limits": pruned_rates,
        "pruned_pairing_sessions": pruned_pairs,
        "pruned_sessions": pruned_sessions,
    }
