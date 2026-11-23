"""Cryptographic primitives for Direct Play Client API authentication.

Implements standard scrypt password hashing, constant-time verification,
anti-enumeration dummy comparisons, high-entropy token generation,
and SHA-256 digest hashing for token storage at rest.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets


class TokenValidationError(ValueError):
    """Raised when an untrusted token has invalid encoding or bounds."""
    status_code: int = 400


# OWASP recommended scrypt parameters: N=16384 (2^14), r=8, p=1
SCRYPT_N = 16384
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_SALT_BYTES = 16
SCRYPT_DKLEN = 64

# Precomputed dummy hash for timing-attack / enumeration mitigation
_DUMMY_SALT = b"\x00" * SCRYPT_SALT_BYTES
_DUMMY_DK = hashlib.scrypt(
    b"timing-mitigation-dummy-password",
    salt=_DUMMY_SALT,
    n=SCRYPT_N,
    r=SCRYPT_R,
    p=SCRYPT_P,
    maxmem=0,
    dklen=SCRYPT_DKLEN,
)
_DUMMY_HASH = (
    f"$scrypt$ln=14,r={SCRYPT_R},p={SCRYPT_P}$"
    f"{base64.b64encode(_DUMMY_SALT).decode('ascii')}$"
    f"{base64.b64encode(_DUMMY_DK).decode('ascii')}"
)


def hash_password(password: str, salt: bytes | None = None) -> str:
    """Hash a plaintext password using standard scrypt."""
    if not isinstance(password, str):
        raise TypeError("Password must be a string")
    if salt is None:
        salt = secrets.token_bytes(SCRYPT_SALT_BYTES)
    elif len(salt) < 16:
        raise ValueError("Salt must be at least 16 bytes")

    dk = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        maxmem=0,
        dklen=SCRYPT_DKLEN,
    )
    salt_b64 = base64.b64encode(salt).decode("ascii")
    dk_b64 = base64.b64encode(dk).decode("ascii")
    return f"$scrypt$ln=14,r={SCRYPT_R},p={SCRYPT_P}${salt_b64}${dk_b64}"


def verify_password(password: str, password_hash: str) -> bool:
    """Verify password against a scrypt hash using constant-time comparison."""
    if not isinstance(password, str) or not isinstance(password_hash, str):
        return False
    try:
        parts = password_hash.split("$")
        # Format: ["", "scrypt", "ln=14,r=8,p=1", salt_b64, dk_b64]
        if len(parts) != 5 or parts[1] != "scrypt":
            return False
        params = dict(kv.split("=") for kv in parts[2].split(","))
        n = 1 << int(params.get("ln", 14))
        r = int(params.get("r", 8))
        p = int(params.get("p", 1))
        salt = base64.b64decode(parts[3].encode("ascii"))
        expected_dk = base64.b64decode(parts[4].encode("ascii"))

        actual_dk = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=n,
            r=r,
            p=p,
            maxmem=0,
            dklen=len(expected_dk),
        )
        return hmac.compare_digest(actual_dk, expected_dk)
    except Exception:
        return False


def dummy_verify(password: str) -> None:
    """Perform a constant-time verification against a dummy hash to prevent enumeration."""
    verify_password(password, _DUMMY_HASH)


def generate_token(prefix: str = "ah_tok_") -> str:
    """Generate an opaque high-entropy URL-safe token."""
    return f"{prefix}{secrets.token_hex(24)}"


def generate_pairing_secret() -> str:
    """Generate an opaque high-entropy polling secret for TV pairing."""
    return f"pair_sec_{secrets.token_hex(24)}"


def generate_pairing_code() -> str:
    """Generate a 6-digit TV pairing code formatted as NNN-NNN."""
    part1 = secrets.randbelow(900) + 100
    part2 = secrets.randbelow(900) + 100
    return f"{part1:03d}-{part2:03d}"


def normalize_pairing_code(code: str) -> str:
    """Normalize a pairing code for lookup by removing whitespace and hyphens."""
    if not isinstance(code, str):
        return ""
    return code.replace("-", "").replace(" ", "").strip().upper()


def hash_token(token: str) -> str:
    """Compute SHA-256 digest of a token for secure storage at rest.

    Validates that untrusted input is bounded ASCII; raises TokenValidationError
    so untrusted non-ASCII or oversized inputs are handled as 4xx, not ASCII 500.
    """
    if not isinstance(token, str):
        raise TokenValidationError("Token must be a string")
    if "\x00" in token:
        raise TokenValidationError("Token contains null characters")
    if len(token) > 256:
        raise TokenValidationError("Token exceeds maximum length")
    try:
        raw = token.encode("ascii")
    except UnicodeEncodeError:
        raise TokenValidationError("Token contains non-ASCII characters")
    return hashlib.sha256(raw).hexdigest()
