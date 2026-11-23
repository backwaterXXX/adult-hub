"""Client API authentication and pairing package."""

from common.client_auth.crypto import (
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
from common.client_auth.schema import ensure_auth_schema
from common.client_auth.service import (
    AuthenticationFailed,
    AuthError,
    BoundedValidationError,
    PairingAlreadyExchanged,
    PairingNotFoundOrExpired,
    RateLimitExceeded,
    approve_pairing,
    authenticate_by_password,
    bootstrap_owner,
    initiate_pairing,
    poll_pairing_status,
    refresh_session_token,
    revoke_session_token,
    verify_access_token,
)

__all__ = [
    "AuthenticationFailed",
    "AuthError",
    "BoundedValidationError",
    "PairingAlreadyExchanged",
    "PairingNotFoundOrExpired",
    "RateLimitExceeded",
    "approve_pairing",
    "authenticate_by_password",
    "bootstrap_owner",
    "check_and_increment_rate_limit",
    "dummy_verify",
    "ensure_auth_schema",
    "generate_pairing_code",
    "generate_pairing_secret",
    "generate_token",
    "hash_password",
    "hash_token",
    "initiate_pairing",
    "normalize_pairing_code",
    "poll_pairing_status",
    "refresh_session_token",
    "revoke_session_token",
    "verify_access_token",
    "verify_password",
]
