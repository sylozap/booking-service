"""Typed view of the claims of a verified token.

Raw JWT claims are parsed once into :class:`Principal`, so the rest of the
service never reads the claims dictionary. These are wire types and carry no
rules of any service.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

__all__ = [
    "ACCESS_TOKEN_TYPE",
    "SERVICE_TOKEN_TYPE",
    "Principal",
    "RoleClaim",
    "principal_from_claims",
]

# The ``typ`` claim, which keeps user and service tokens from being
# interchangeable. noqa: these name a kind of token, not credentials.
ACCESS_TOKEN_TYPE = "access"  # noqa: S105
SERVICE_TOKEN_TYPE = "service"  # noqa: S105


@dataclass(frozen=True, slots=True)
class RoleClaim:
    """One role the token asserts, and the salon it is limited to, if any."""

    role: str
    salon_id: UUID | None = None

    @property
    def is_global(self) -> bool:
        """Whether the role applies everywhere rather than inside one salon."""
        return self.salon_id is None


@dataclass(frozen=True, slots=True)
class Principal:
    """Who is calling, according to a token whose signature has been checked.

    ``subject`` is a user id for an access token and a client id for a service
    token; ``token_type`` says which, and every dependency that cares checks it
    rather than guessing from the shape of the subject.
    """

    subject: str
    token_type: str
    roles: tuple[RoleClaim, ...] = ()
    scopes: tuple[str, ...] = ()
    token_id: UUID | None = None
    expires_at: datetime | None = None

    @property
    def is_service(self) -> bool:
        """Whether this is a service calling another service."""
        return self.token_type == SERVICE_TOKEN_TYPE

    @property
    def user_id(self) -> UUID:
        """The subject as a user identifier.

        Raises for a service token: a client id is not a user id, and code that
        confuses them writes rows owned by a service that has no owner.
        """
        if self.token_type != ACCESS_TOKEN_TYPE:
            raise ValueError(f"a {self.token_type} token has no user id")
        return UUID(self.subject)

    def has_any_role(self, roles: frozenset[str]) -> bool:
        """Whether the caller holds any of these roles, anywhere.

        Deliberately not salon-aware. "Which salon" is a question about the
        resource being touched, which a generic dependency cannot see; the
        scenario that knows the salon checks :meth:`holds` itself.
        """
        return any(grant.role in roles for grant in self.roles)

    def holds(self, role: str, *, salon_id: UUID | None = None) -> bool:
        """Whether the caller holds this role, globally or in this salon.

        A global grant counts in every salon; a scoped grant only in its own.
        """
        for grant in self.roles:
            if grant.role != role:
                continue
            if grant.is_global or grant.salon_id == salon_id:
                return True
        return False


def principal_from_claims(claims: dict[str, object]) -> Principal:
    """Turn verified claims into a :class:`Principal`.

    Called only after the token has been verified. A malformed ``roles`` entry
    is dropped, while a missing ``sub`` or ``typ`` is an error.
    """
    subject = claims.get("sub")
    token_type = claims.get("typ")
    if not isinstance(subject, str) or not isinstance(token_type, str):
        raise ValueError("token does not say who it belongs to")

    return Principal(
        subject=subject,
        token_type=token_type,
        roles=_roles_of(claims.get("roles")),
        scopes=_strings_of(claims.get("scopes")),
        token_id=_uuid_or_none(claims.get("jti")),
        expires_at=_moment_or_none(claims.get("exp")),
    )


def _roles_of(value: object) -> tuple[RoleClaim, ...]:
    """Parse the ``roles`` claim, skipping entries that make no sense."""
    if not isinstance(value, list):
        return ()

    grants = []
    for entry in value:
        if not isinstance(entry, dict):
            continue
        role = entry.get("role")
        if not isinstance(role, str):
            continue
        grants.append(RoleClaim(role=role, salon_id=_uuid_or_none(entry.get("salon_id"))))
    return tuple(grants)


def _strings_of(value: object) -> tuple[str, ...]:
    """Parse a claim that carries a list of strings."""
    if not isinstance(value, list):
        return ()
    return tuple(entry for entry in value if isinstance(entry, str))


def _uuid_or_none(value: object) -> UUID | None:
    if not isinstance(value, str):
        return None
    try:
        return UUID(value)
    except ValueError:
        return None


def _moment_or_none(value: object) -> datetime | None:
    """A NumericDate as an aware datetime, or nothing."""
    if not isinstance(value, int | float) or isinstance(value, bool):
        return None
    return datetime.fromtimestamp(value, tz=UTC)
