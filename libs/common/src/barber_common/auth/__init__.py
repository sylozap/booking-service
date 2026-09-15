"""Verification of the tokens issued by ``auth``.

Every service verifies tokens itself instead of trusting the gateway. Issuing
tokens stays in ``auth``, which alone holds the private key.
"""

from barber_common.auth.claims import (
    ACCESS_TOKEN_TYPE,
    SERVICE_TOKEN_TYPE,
    Principal,
    RoleClaim,
    principal_from_claims,
)
from barber_common.auth.dependencies import (
    current_principal,
    current_user,
    require_roles,
    require_service_token,
    use_authentication,
)
from barber_common.auth.jwks import JwksClient, jwks_verifier, refreshing
from barber_common.auth.keys import StaticKeys, UnknownSigningKey, VerificationKeys
from barber_common.auth.verifier import InvalidToken, TokenVerifier

__all__ = [
    "ACCESS_TOKEN_TYPE",
    "SERVICE_TOKEN_TYPE",
    "InvalidToken",
    "JwksClient",
    "Principal",
    "RoleClaim",
    "StaticKeys",
    "TokenVerifier",
    "UnknownSigningKey",
    "VerificationKeys",
    "current_principal",
    "current_user",
    "jwks_verifier",
    "principal_from_claims",
    "refreshing",
    "require_roles",
    "require_service_token",
    "use_authentication",
]
