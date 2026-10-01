"""Which service answers which path.

Not "by the first segment of the path", although that is how it started: the
master lives in two services. The profile, the services offered and the
activation belong to ``catalog``; the schedule and the settings of the same
master belong to ``booking`` (``docs/03-services.md``, "Где живёт мастер"). So
the table holds path templates, and it is read top to bottom: the first rule
that matches wins, which is why the two ``booking`` rules under ``/masters``
stand above the ``catalog`` rule for ``/masters`` itself.

A rule covers its template and everything below it, segment by segment:
``/api/v1/bookings`` matches ``/api/v1/bookings/{id}/cancel`` and does not match
``/api/v1/bookingsx``.

``/internal/*`` is in no rule and therefore not reachable from outside: those
endpoints take service tokens only, and the gateway never carries one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum

__all__ = ["ROUTES", "PathPattern", "Route", "Upstream", "route_for"]


class Upstream(StrEnum):
    """A service behind the gateway, named as in its settings and in logs."""

    AUTH = "auth"
    CATALOG = "catalog"
    BOOKING = "booking"
    NOTIFICATION = "notification"


# What a placeholder such as {master_id} stands for: exactly one segment.
_PLACEHOLDER = re.compile(r"\{[^/{}]+\}")


@dataclass(frozen=True, slots=True)
class PathPattern:
    """A path template of the OpenAPI kind, compiled once.

    ``exact`` decides whether the template has to be the whole path or may be
    followed by more segments.
    """

    template: str
    exact: bool = False
    _regex: re.Pattern[str] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        body = "[^/]+".join(re.escape(part) for part in _PLACEHOLDER.split(self.template))
        tail = "" if self.exact else "(?:/.*)?"
        # object.__setattr__: the dataclass is frozen, and the regex is derived
        # from the template rather than given.
        object.__setattr__(self, "_regex", re.compile(f"^{body}{tail}$"))

    def matches(self, path: str) -> bool:
        return self._regex.match(path) is not None


@dataclass(frozen=True, slots=True)
class Route:
    """One rule of the table: a path template and the service behind it."""

    pattern: PathPattern
    upstream: Upstream


def _route(template: str, upstream: Upstream) -> Route:
    return Route(pattern=PathPattern(template), upstream=upstream)


ROUTES: tuple[Route, ...] = (
    # auth
    _route("/api/v1/auth", Upstream.AUTH),
    _route("/api/v1/users", Upstream.AUTH),
    _route("/.well-known/jwks.json", Upstream.AUTH),
    # The schedule and the settings of a master are booking's. They stand above
    # the catalog rule for /masters, which would otherwise take them.
    _route("/api/v1/masters/{master_id}/schedule", Upstream.BOOKING),
    _route("/api/v1/masters/{master_id}/settings", Upstream.BOOKING),
    # catalog
    _route("/api/v1/masters", Upstream.CATALOG),
    _route("/api/v1/salons", Upstream.CATALOG),
    _route("/api/v1/services", Upstream.CATALOG),
    # booking
    _route("/api/v1/availability", Upstream.BOOKING),
    _route("/api/v1/bookings", Upstream.BOOKING),
    # notification
    _route("/api/v1/notifications", Upstream.NOTIFICATION),
)


def route_for(path: str) -> Upstream | None:
    """The service that answers this path, or ``None`` when none does."""
    for route in ROUTES:
        if route.pattern.matches(path):
            return route.upstream
    return None
