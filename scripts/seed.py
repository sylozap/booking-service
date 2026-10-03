#!/usr/bin/env python
"""Fill the platform with demo data through its public API.

Three salons in three time zones, twelve masters, fifteen services, weekly
schedules with days off over the coming month, a hundred clients and bookings
over the next two weeks, a few of them cancelled.

Usage::

    make seed                   # the compose stack, on its local ports
    SEED_AUTH_URL=... python scripts/seed.py

In a cluster the same script runs as a Job (deploy/helm/service/templates/
seed-job.yaml) against the Services of the namespace.

**Through the API, never into a database.** Writing rows directly would skip
the validation of the services and the events they publish, and leave states
the platform cannot reach by itself. The script talks to auth, catalog and
booking directly rather than through the gateway: the gateway limits
registrations to three an hour per address and bookings to five a minute per
caller, which is right for a person and makes a seed of a hundred clients
impossible. The endpoints are the same ones the gateway forwards to.

The administrator it signs in as is the first ``super_admin`` auth creates
from configuration (BOOTSTRAP_ADMIN_*). Bookings are made by that
administrator on behalf of the clients, as a salon books a client who called.

**Running it again changes nothing.** Salons, services and masters are found
by name and created only when missing. A client that already exists answers
``409`` without its id, so a second run cannot tell whose bookings to make:
bookings are created only in the run that registered their clients. The stand
has no persistent storage, so in practice the seed always meets an empty
platform; the second run is for a ``make kind-up`` repeated on a live one.

What is created is decided by a fixed random seed, so two stands seeded on the
same day look alike.
"""

from __future__ import annotations

import asyncio
import random
import secrets
import sys
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import httpx
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# --- what the demo consists of -----------------------------------------------

SALON_COUNT = 3
MASTERS_PER_SALON = 4
SERVICES_PER_SALON = 5
CLIENT_COUNT = 100
# How far ahead schedules get days off, and bookings are made.
SCHEDULE_DAYS = 30
BOOKING_DAYS = 14
# Bookings per master over the two weeks: busy enough for the dashboards,
# free enough for the load test to find slots.
BOOKINGS_PER_MASTER = 12
# Share of the bookings cancelled by the salon afterwards, so that the business
# dashboard shows more than one status.
CANCELLED_SHARE = 0.1
RANDOM_SEED = 20261002

DEMO_DOMAIN = "demo.barber.example"


@dataclass(frozen=True, slots=True)
class SalonPlan:
    name: str
    city: str
    address: str
    phone: str
    timezone: str


@dataclass(frozen=True, slots=True)
class ServicePlan:
    name: str
    duration_min: int
    price: Decimal


@dataclass(frozen=True, slots=True)
class WorkingDay:
    """Hours of one weekday, 0 is Monday, in the salon's wall clock."""

    weekday: int
    start: str
    end: str


@dataclass(frozen=True, slots=True)
class Offer:
    """A service a master provides, with what differs from the salon's price."""

    service: str
    price_override: Decimal | None = None


@dataclass(frozen=True, slots=True)
class MasterPlan:
    salon: str
    display_name: str
    specialization: str
    email: str
    phone: str
    offers: tuple[Offer, ...]
    week: tuple[WorkingDay, ...]
    # Days off counted from the day the seed runs; never today or tomorrow, so
    # they are in the future in every time zone of the demo.
    days_off_in: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class ClientPlan:
    email: str
    phone: str


@dataclass(frozen=True, slots=True)
class Plan:
    salons: tuple[SalonPlan, ...]
    services: dict[str, tuple[ServicePlan, ...]]
    masters: tuple[MasterPlan, ...]
    clients: tuple[ClientPlan, ...]


SALONS = (
    SalonPlan(
        name="Barber Patriki",
        city="Moscow",
        address="Malaya Bronnaya 28",
        phone="+74950000001",
        timezone="Europe/Moscow",
    ),
    SalonPlan(
        name="Barber Vaynera",
        city="Yekaterinburg",
        address="Vaynera 9",
        phone="+73430000001",
        timezone="Asia/Yekaterinburg",
    ),
    SalonPlan(
        name="Barber Akademgorodok",
        city="Novosibirsk",
        address="Morskoy prospekt 2",
        phone="+73830000001",
        timezone="Asia/Novosibirsk",
    ),
)

# Five services per salon; the price scales with the city.
SERVICE_MENU = (
    ("Haircut", 45, Decimal("2500")),
    ("Beard trim", 30, Decimal("1500")),
    ("Haircut and beard", 75, Decimal("3500")),
    ("Buzz cut", 30, Decimal("1200")),
    ("Kids haircut", 30, Decimal("1500")),
)
PRICE_FACTOR = {
    "Moscow": Decimal("1.4"),
    "Yekaterinburg": Decimal("1.0"),
    "Novosibirsk": Decimal("0.9"),
}

MASTER_NAMES = (
    "Artem", "Boris", "Vlad", "Gleb", "Denis", "Egor",
    "Zakhar", "Ilya", "Kirill", "Lev", "Mark", "Nikita",
)  # fmt: skip
SPECIALIZATIONS = ("Classic cuts", "Beards", "Fades", "Kids")

# Shifts a master may work, in the salon's wall clock.
SHIFTS = (
    ((0, 1, 2, 3, 4), "10:00", "19:00"),
    ((1, 2, 3, 4, 5), "11:00", "20:00"),
    ((0, 2, 4, 5), "09:00", "18:00"),
    ((2, 3, 4, 5, 6), "12:00", "21:00"),
)


def build_plan(rng: random.Random | None = None) -> Plan:
    """The whole demo, decided up front by a fixed seed."""
    rng = rng or random.Random(RANDOM_SEED)  # noqa: S311 - demo data, not a secret

    services = {
        salon.name: tuple(
            ServicePlan(
                name=name,
                duration_min=duration,
                price=(price * PRICE_FACTOR[salon.city]).quantize(Decimal("1")),
            )
            for name, duration, price in SERVICE_MENU
        )
        for salon in SALONS
    }

    masters = []
    for number, display_name in enumerate(MASTER_NAMES):
        salon = SALONS[number // MASTERS_PER_SALON]
        menu = services[salon.name]
        offered = rng.sample(menu, k=rng.randint(3, len(menu)))
        offers = tuple(
            Offer(
                service=service.name,
                # A third of the offers cost more with this master.
                price_override=(service.price + Decimal("300")) if rng.random() < 0.33 else None,
            )
            for service in sorted(offered, key=lambda s: s.name)
        )
        weekdays, start, end = SHIFTS[number % len(SHIFTS)]
        masters.append(
            MasterPlan(
                salon=salon.name,
                display_name=display_name,
                specialization=SPECIALIZATIONS[number % len(SPECIALIZATIONS)],
                email=f"master-{number + 1:02d}@{DEMO_DOMAIN}",
                phone=f"+7999200{number + 1:04d}",
                offers=offers,
                week=tuple(WorkingDay(weekday, start, end) for weekday in weekdays),
                days_off_in=tuple(sorted(rng.sample(range(2, SCHEDULE_DAYS), k=2))),
            )
        )

    clients = tuple(
        ClientPlan(email=f"client-{n:03d}@{DEMO_DOMAIN}", phone=f"+7999100{n:04d}")
        for n in range(1, CLIENT_COUNT + 1)
    )
    return Plan(salons=SALONS, services=services, masters=tuple(masters), clients=clients)


@dataclass(frozen=True, slots=True)
class Candidate:
    """A start the platform said is free, for one service of one master."""

    service_id: UUID
    start: datetime
    duration_min: int

    @property
    def end(self) -> datetime:
        return self.start + timedelta(minutes=self.duration_min)


def choose_bookings(
    candidates: Sequence[Candidate], count: int, rng: random.Random
) -> list[Candidate]:
    """Up to ``count`` free starts of one master that do not overlap each other.

    Availability lists every free start, and the starts of one master overlap
    each other: a 45-minute service listed at 10:00 and at 10:15 cannot be
    booked at both. The database would refuse the second with 409 anyway, but
    a seed that knows the answer does not ask.
    """
    shuffled = list(candidates)
    rng.shuffle(shuffled)
    chosen: list[Candidate] = []
    for candidate in shuffled:
        if len(chosen) == count:
            break
        if all(candidate.end <= other.start or other.end <= candidate.start for other in chosen):
            chosen.append(candidate)
    return sorted(chosen, key=lambda c: c.start)


# --- talking to the platform -------------------------------------------------


class SeedSettings(BaseSettings):
    """Where the services are, and who to sign in as. Read from SEED_*."""

    model_config = SettingsConfigDict(env_prefix="SEED_")

    auth_url: str = "http://localhost:8001"
    catalog_url: str = "http://localhost:8002"
    booking_url: str = "http://localhost:8003"
    # The first super_admin of auth (BOOTSTRAP_ADMIN_*). No default for the
    # password: it is a credential.
    admin_email: str = "admin@barber.example"
    admin_password: SecretStr
    # How long the services may take to become ready -- in a cluster the Job
    # can start before the last of them.
    ready_timeout_seconds: float = 300.0
    # Requests in flight at once: registration hashes a password with argon2,
    # and a hundred at once would only queue inside auth.
    concurrency: int = 4


class SeedError(Exception):
    """The platform answered something the seed cannot go on from."""


@dataclass
class Report:
    """What a run found and what it created, printed at the end."""

    created: dict[str, int] = field(default_factory=dict)
    found: dict[str, int] = field(default_factory=dict)

    def add(self, kind: str, *, created: bool) -> None:
        target = self.created if created else self.found
        target[kind] = target.get(kind, 0) + 1

    def lines(self) -> list[str]:
        kinds = sorted(set(self.created) | set(self.found))
        return [
            f"{kind:<16} created {self.created.get(kind, 0):>4}, "
            f"already there {self.found.get(kind, 0):>4}"
            for kind in kinds
        ]


def log(message: str) -> None:
    print(message, flush=True)


class Platform:
    """The three services the seed talks to, with the administrator's token."""

    def __init__(self, settings: SeedSettings, transport: httpx.AsyncBaseTransport | None = None):
        self._settings = settings
        timeout = httpx.Timeout(30.0)
        self.auth = httpx.AsyncClient(
            base_url=settings.auth_url, timeout=timeout, transport=transport
        )
        self.catalog = httpx.AsyncClient(
            base_url=settings.catalog_url, timeout=timeout, transport=transport
        )
        self.booking = httpx.AsyncClient(
            base_url=settings.booking_url, timeout=timeout, transport=transport
        )
        self._token: str | None = None

    async def close(self) -> None:
        for client in (self.auth, self.catalog, self.booking):
            await client.aclose()

    async def wait_until_ready(self) -> None:
        """Poll /health/ready of every service until it answers 200."""
        deadline = time.monotonic() + self._settings.ready_timeout_seconds
        for client in (self.auth, self.catalog, self.booking):
            while True:
                try:
                    response = await client.get("/health/ready")
                    if response.status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                if time.monotonic() > deadline:
                    raise SeedError(f"{client.base_url} is not ready")
                await asyncio.sleep(2)

    async def sign_in(self) -> None:
        """A fresh token for the administrator.

        Called before every phase: an access token lives fifteen minutes, and a
        seed against a slow stand can outlive one.
        """
        response = await self.call(
            self.auth,
            "POST",
            "/api/v1/auth/login",
            json={
                "email": self._settings.admin_email,
                "password": self._settings.admin_password.get_secret_value(),
            },
            expect={200},
            authorized=False,
        )
        self._token = response.json()["access_token"]

    async def call(
        self,
        client: httpx.AsyncClient,
        method: str,
        path: str,
        *,
        expect: Iterable[int],
        json: object = None,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        authorized: bool = True,
    ) -> httpx.Response:
        """One request; an unexpected status stops the seed with what it said."""
        all_headers = dict(headers or {})
        if authorized and self._token is not None:
            all_headers["Authorization"] = f"Bearer {self._token}"
        response = await client.request(method, path, json=json, params=params, headers=all_headers)
        if response.status_code not in set(expect):
            raise SeedError(f"{method} {path}: {response.status_code} {response.text[:500]}")
        return response

    async def pages(self, client: httpx.AsyncClient, path: str) -> list[dict[str, Any]]:
        """Every item of a paginated list."""
        items: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            params = {"limit": "100"}
            if cursor:
                params["cursor"] = cursor
            page = (await self.call(client, "GET", path, expect={200}, params=params)).json()
            items.extend(page["items"])
            cursor = page.get("next_cursor")
            if not cursor:
                return items


@dataclass
class Seeded:
    """Identifiers of what exists after the catalog phase."""

    salons: dict[str, UUID] = field(default_factory=dict)
    services: dict[tuple[str, str], UUID] = field(default_factory=dict)
    masters: dict[str, UUID] = field(default_factory=dict)
    clients: list[UUID] = field(default_factory=list)


class Seeder:
    """Brings the platform to the plan: finds what is there, creates the rest."""

    def __init__(
        self,
        platform: Platform,
        *,
        plan: Plan | None = None,
        concurrency: int = 4,
        today: date | None = None,
    ) -> None:
        self._platform = platform
        self._plan = plan or build_plan()
        self._limit = asyncio.Semaphore(concurrency)
        self._today = today or datetime.now(UTC).date()
        self._rng = random.Random(RANDOM_SEED)  # noqa: S311 - demo data, not a secret
        self.report = Report()

    async def run(self) -> Report:
        platform = self._platform
        await platform.wait_until_ready()
        seeded = Seeded()

        await platform.sign_in()
        await self._salons(seeded)
        await self._services(seeded)
        await self._masters(seeded)

        await platform.sign_in()
        await self._schedules(seeded)
        await self._clients(seeded)

        await platform.sign_in()
        await self._bookings(seeded)
        return self.report

    # --- catalog --------------------------------------------------------------

    async def _salons(self, seeded: Seeded) -> None:
        platform = self._platform
        existing = {
            s["name"]: UUID(s["id"])
            for s in await platform.pages(platform.catalog, "/api/v1/salons")
        }
        for salon in self._plan.salons:
            if salon.name in existing:
                seeded.salons[salon.name] = existing[salon.name]
                self.report.add("salons", created=False)
                continue
            response = await platform.call(
                platform.catalog,
                "POST",
                "/api/v1/salons",
                json={
                    "name": salon.name,
                    "city": salon.city,
                    "address": salon.address,
                    "phone": salon.phone,
                    "timezone": salon.timezone,
                },
                expect={201},
            )
            seeded.salons[salon.name] = UUID(response.json()["id"])
            self.report.add("salons", created=True)

    async def _services(self, seeded: Seeded) -> None:
        platform = self._platform
        for salon_name, salon_id in seeded.salons.items():
            path = f"/api/v1/salons/{salon_id}/services"
            existing = {
                s["name"]: UUID(s["id"]) for s in await platform.pages(platform.catalog, path)
            }
            for service in self._plan.services[salon_name]:
                if service.name in existing:
                    seeded.services[(salon_name, service.name)] = existing[service.name]
                    self.report.add("services", created=False)
                    continue
                response = await platform.call(
                    platform.catalog,
                    "POST",
                    path,
                    json={
                        "name": service.name,
                        "base_duration_min": service.duration_min,
                        "base_price": str(service.price),
                        "currency": "RUB",
                    },
                    expect={201},
                )
                seeded.services[(salon_name, service.name)] = UUID(response.json()["id"])
                self.report.add("services", created=True)

    async def _masters(self, seeded: Seeded) -> None:
        platform = self._platform
        existing: dict[str, UUID] = {}
        for salon_id in seeded.salons.values():
            masters = await platform.pages(platform.catalog, f"/api/v1/salons/{salon_id}/masters")
            existing |= {m["display_name"]: UUID(m["id"]) for m in masters}

        for master in self._plan.masters:
            if master.display_name in existing:
                seeded.masters[master.display_name] = existing[master.display_name]
                self.report.add("masters", created=False)
            else:
                master_id = await self._create_master(master, seeded)
                if master_id is None:
                    continue
                seeded.masters[master.display_name] = master_id
                self.report.add("masters", created=True)

            # Idempotent by design (PUT): the offers are put every run.
            for offer in master.offers:
                service_id = seeded.services[(master.salon, offer.service)]
                await platform.call(
                    platform.catalog,
                    "PUT",
                    f"/api/v1/masters/{seeded.masters[master.display_name]}/services/{service_id}",
                    json={
                        "price_override": None
                        if offer.price_override is None
                        else str(offer.price_override)
                    },
                    expect={200, 201},
                )

    async def _create_master(self, master: MasterPlan, seeded: Seeded) -> UUID | None:
        platform = self._platform
        user_id = await self._register(master.email, master.phone)
        if user_id is None:
            # The account exists and its profile does not: a run before this
            # one stopped between the two. Its id cannot be learnt from the
            # API, so this master is left out rather than guessed.
            log(f"master {master.display_name}: the account exists without a profile, skipped")
            return None
        salon_id = seeded.salons[master.salon]
        response = await platform.call(
            platform.catalog,
            "POST",
            "/api/v1/masters",
            json={
                "salon_id": str(salon_id),
                "user_id": str(user_id),
                "display_name": master.display_name,
                "specialization": master.specialization,
            },
            expect={201},
        )
        # The role makes the master's own bookings visible to them.
        await platform.call(
            platform.auth,
            "POST",
            f"/api/v1/users/{user_id}/roles",
            json={"role": "master", "salon_id": str(salon_id)},
            expect={201},
        )
        return UUID(response.json()["id"])

    async def _register(self, email: str, phone: str) -> UUID | None:
        """A new account, or None for one that exists already.

        The password is random and thrown away. A demo account never signs in:
        its address is never confirmed, and its bookings are made by the
        administrator. A known password would be one more credential in git.
        """
        async with self._limit:
            response = await self._platform.call(
                self._platform.auth,
                "POST",
                "/api/v1/auth/register",
                json={"email": email, "phone": phone, "password": _throwaway_password()},
                expect={201, 409},
                authorized=False,
            )
        if response.status_code == 409:
            return None
        return UUID(response.json()["user_id"])

    # --- booking --------------------------------------------------------------

    async def _schedules(self, seeded: Seeded) -> None:
        platform = self._platform
        for master in self._plan.masters:
            master_id = seeded.masters.get(master.display_name)
            if master_id is None:
                continue
            path = f"/api/v1/masters/{master_id}/schedule"
            current = (await platform.call(platform.booking, "GET", path, expect={200})).json()
            if current["versions"]:
                self.report.add("schedules", created=False)
            else:
                # valid_from left out: from today in the salon's own calendar.
                await platform.call(
                    platform.booking,
                    "PUT",
                    path,
                    json={
                        "intervals": [
                            {"weekday": d.weekday, "start_time": d.start, "end_time": d.end}
                            for d in master.week
                        ]
                    },
                    expect={200},
                )
                self.report.add("schedules", created=True)

            exceptions = await platform.call(
                platform.booking, "GET", f"{path}/exceptions", expect={200}
            )
            if exceptions.json()["items"]:
                continue
            for days in master.days_off_in:
                await platform.call(
                    platform.booking,
                    "POST",
                    f"{path}/exceptions",
                    json={
                        "effective_on": (self._today + timedelta(days=days)).isoformat(),
                        "kind": "day_off",
                        "reason": "Demo day off",
                    },
                    expect={201},
                )
                self.report.add("days off", created=True)

    async def _clients(self, seeded: Seeded) -> None:
        results = await asyncio.gather(
            *(self._register(client.email, client.phone) for client in self._plan.clients)
        )
        for user_id in results:
            self.report.add("clients", created=user_id is not None)
            if user_id is not None:
                seeded.clients.append(user_id)

    async def _bookings(self, seeded: Seeded) -> None:
        if not seeded.clients:
            log("bookings: no client was registered in this run, so none are made")
            return
        platform = self._platform
        # From tomorrow: "today" in UTC is already tomorrow in Novosibirsk for
        # part of the day, and the salon's calendar is the one asked about.
        date_from = self._today + timedelta(days=1)
        date_to = date_from + timedelta(days=BOOKING_DAYS - 1)

        made: list[UUID] = []
        for master in self._plan.masters:
            master_id = seeded.masters.get(master.display_name)
            if master_id is None:
                continue
            candidates = []
            for offer in master.offers:
                service_id = seeded.services[(master.salon, offer.service)]
                answer = (
                    await platform.call(
                        platform.booking,
                        "GET",
                        "/api/v1/availability",
                        params={
                            "master_id": str(master_id),
                            "service_id": str(service_id),
                            "date_from": date_from.isoformat(),
                            "date_to": date_to.isoformat(),
                        },
                        expect={200},
                    )
                ).json()
                candidates += [
                    Candidate(
                        service_id=service_id,
                        start=datetime.fromisoformat(slot),
                        duration_min=answer["duration_min"],
                    )
                    for day in answer["days"]
                    for slot in day["slots"]
                ]

            for candidate in choose_bookings(candidates, BOOKINGS_PER_MASTER, self._rng):
                booking_id = await self._book(
                    master_id, candidate, self._rng.choice(seeded.clients)
                )
                if booking_id is not None:
                    made.append(booking_id)

        for booking_id in self._rng.sample(made, k=int(len(made) * CANCELLED_SHARE)):
            await platform.call(
                platform.booking,
                "POST",
                f"/api/v1/bookings/{booking_id}/cancel",
                json={"reason": "Demo: the salon rescheduled the master"},
                expect={200},
            )
            self.report.add("cancellations", created=True)

    async def _book(self, master_id: UUID, candidate: Candidate, client_id: UUID) -> UUID | None:
        """Book on behalf of a client; None when the slot went in the meantime."""
        response = await self._platform.call(
            self._platform.booking,
            "POST",
            "/api/v1/bookings",
            json={
                "master_id": str(master_id),
                "service_id": str(candidate.service_id),
                "start_at": candidate.start.isoformat(),
                "client_user_id": str(client_id),
            },
            headers={"Idempotency-Key": str(uuid4())},
            # 409: someone took the slot after availability was read. The
            # stand may be live; the seed is not the only caller.
            expect={201, 409},
        )
        if response.status_code == 409:
            return None
        self.report.add("bookings", created=True)
        return UUID(response.json()["id"])


def _throwaway_password() -> str:
    # A letter and a digit whatever the random part is: the policy of auth.
    return f"demo-{secrets.token_urlsafe(18)}-1"


async def seed(settings: SeedSettings, *, today: date | None = None) -> Report:
    """Run the seed against the platform the settings point at."""
    platform = Platform(settings)
    try:
        return await Seeder(platform, concurrency=settings.concurrency, today=today).run()
    finally:
        await platform.close()


def main() -> int:
    settings = SeedSettings()
    try:
        report = asyncio.run(seed(settings))
    except SeedError as error:
        log(f"seed failed: {error}")
        return 1
    for line in report.lines():
        log(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
