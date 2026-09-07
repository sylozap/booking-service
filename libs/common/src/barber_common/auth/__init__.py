"""Verifying the tokens ``auth`` issues, in every service that receives one.

The gateway checks the signature and so does each service: a request that
reaches a pod without passing the gateway is rejected by the pod itself
(ADR-0010). What is shared here is the checking, never the issuing -- minting a
token needs the private key, and that lives in ``auth`` alone.
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
from barber_common.auth.jwks import JwksClient, refreshing
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
    "principal_from_claims",
    "refreshing",
    "require_roles",
    "require_service_token",
    "use_authentication",
]
