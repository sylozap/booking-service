#!/usr/bin/env python
"""Prepare the clients the load test signs in as.

Registers ``LOADTEST_USERS`` accounts (``load-NNN@load.barber.example``),
confirms their addresses, signs each of them in, and writes the password and
a fresh access token per client to ``loadtest/k6/users.json`` for the k6
scenario to read.

Usage::

    make loadtest-users          # kind: port-forwards auth, reads the logs of notification
    make loadtest                # runs this first, so the tokens are fresh

**The tokens are signed in here, not by k6.** A sign-in through the gateway is
an anonymous request, thirty a minute per address, and k6 is one address: a
hundred clients would take its setup four minutes. An access token lives
fifteen minutes, which bounds a run (loadtest/k6/README.md).

**Why not the demo clients of the seed.** Their passwords are thrown away and
their addresses are never confirmed: they cannot sign in, and nothing reaches
them. The load test needs clients that sign in -- the gateway lets an
anonymous address make thirty requests a minute, a user a hundred and twenty,
and a booking takes a token -- and that receive what they book: the Kafka
drill counts the notifications of the bookings made.

**Auth directly, not the gateway**, for the reason the seed gives: the gateway
lets an address register three accounts an hour.

**The confirmation token comes from the log** of notification. The letter is
written there by the log provider in the ``local`` environment, the one kind
runs in, and its fields are cleared from the journal once it is sent, so the
log is the one place the token is left.

**Running it again creates nothing.** The password is kept in the output file
and reused; an account that exists and is confirmed is only signed in again.
"""

from __future__ import annotations

import asyncio
import json
import re
import secrets
import subprocess
import sys
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import httpx
from pydantic_settings import BaseSettings, SettingsConfigDict

DOMAIN = "load.barber.example"
MESSAGES_LOGGER = "barber_notification.messages"
CONFIRMATION_TEMPLATE = "email_confirmation"
_TOKEN = re.compile(r"[?&]token=([A-Za-z0-9_\-]+)")


class LoadUsersSettings(BaseSettings):
    """Where auth is, how many clients, where to write them. Read from LOADTEST_*."""

    model_config = SettingsConfigDict(env_prefix="LOADTEST_")

    auth_url: str = "http://localhost:18001"
    users: int = 100
    output: Path = Path("loadtest/k6/users.json")
    # Prints the log of notification, one JSON record per line.
    logs_command: str = (
        "kubectl logs --namespace barber --selector app.kubernetes.io/name=notification "
        "--tail=-1 --max-log-requests=10"
    )
    # How long the letters may take to go out: an event through Kafka, then
    # the delivery worker.
    confirmation_timeout_seconds: float = 120.0
    # Registration and sign-in hash with argon2; more at once only queue in auth.
    concurrency: int = 4


class LoadUsersError(Exception):
    """Auth answered something the preparation cannot go on from."""


@dataclass(frozen=True, slots=True)
class LoadUser:
    email: str
    phone: str


def plan_users(count: int) -> list[LoadUser]:
    """The accounts, the same ones on every run.

    The phones are a range of their own: +7999100... are the clients of the
    seed and +7999200... its masters, and a phone taken answers ``409`` just
    like an address taken.
    """
    return [
        LoadUser(email=f"load-{number:03d}@{DOMAIN}", phone=f"+7999300{number:04d}")
        for number in range(1, count + 1)
    ]


def confirmation_tokens(log_lines: Iterable[str]) -> dict[str, str]:
    """The latest confirmation token of every address the log shows a letter to."""
    tokens: dict[str, str] = {}
    for line in log_lines:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict):
            continue
        if record.get("logger") != MESSAGES_LOGGER:
            continue
        if record.get("template") != CONFIRMATION_TEMPLATE:
            continue
        address, body = record.get("address"), record.get("body")
        if not isinstance(address, str) or not isinstance(body, str):
            continue
        found = _TOKEN.search(body)
        if found is not None:
            tokens[address] = found.group(1)
    return tokens


def read_password(output: Path) -> str:
    """The password of an earlier run, or a new one."""
    if output.exists():
        stored = json.loads(output.read_text())
        password = stored.get("password")
        if isinstance(password, str) and password:
            return password
    return secrets.token_urlsafe(18)


def log(message: str) -> None:
    print(message, flush=True)


class Preparation:
    def __init__(
        self,
        settings: LoadUsersSettings,
        *,
        password: str,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self._password = password
        self._auth = httpx.AsyncClient(
            base_url=settings.auth_url, timeout=httpx.Timeout(30.0), transport=transport
        )
        self._limit = asyncio.Semaphore(settings.concurrency)

    async def close(self) -> None:
        await self._auth.aclose()

    async def run(self, users: list[LoadUser]) -> dict[str, str]:
        """An access token for every account, signed in after it is ready."""
        await asyncio.gather(*(self._register(user) for user in users))
        tokens = await self._sign_in_all(users)
        pending = [user for user in users if user.email not in tokens]
        log(f"registered {len(users)}, {len(pending)} to confirm")
        if pending:
            letters = await self._wait_for_tokens({user.email for user in pending})
            await asyncio.gather(*(self._confirm(letters[user.email]) for user in pending))
            tokens |= await self._sign_in_all(pending)
            unconfirmed = [user for user in pending if user.email not in tokens]
            if unconfirmed:
                raise LoadUsersError(f"{unconfirmed[0].email} still cannot sign in")
        return tokens

    async def _register(self, user: LoadUser) -> None:
        async with self._limit:
            response = await self._auth.post(
                "/api/v1/auth/register",
                json={"email": user.email, "phone": user.phone, "password": self._password},
            )
        if response.status_code not in {201, 409}:
            raise LoadUsersError(f"register {user.email}: {response.status_code} {response.text}")

    async def _sign_in_all(self, users: list[LoadUser]) -> dict[str, str]:
        """The access tokens of the accounts that sign in, by address."""
        tokens = await asyncio.gather(*(self._sign_in(user) for user in users))
        return {
            user.email: token
            for user, token in zip(users, tokens, strict=True)
            if token is not None
        }

    async def _sign_in(self, user: LoadUser) -> str | None:
        """The access token, or None while the address waits for confirmation."""
        async with self._limit:
            response = await self._auth.post(
                "/api/v1/auth/login", json={"email": user.email, "password": self._password}
            )
        if response.status_code == 200:
            token: str = response.json()["access_token"]
            return token
        if response.status_code == 403:
            return None
        if response.status_code == 401:
            raise LoadUsersError(
                f"{user.email} does not sign in with the password of {self._settings.output}: "
                f"it was registered with another one, or its phone {user.phone} belongs "
                "to another account"
            )
        raise LoadUsersError(f"login {user.email}: {response.status_code} {response.text}")

    async def _wait_for_tokens(self, emails: set[str]) -> dict[str, str]:
        deadline = time.monotonic() + self._settings.confirmation_timeout_seconds
        while True:
            tokens = confirmation_tokens(await self._read_logs())
            missing = emails - tokens.keys()
            if not missing:
                return tokens
            if time.monotonic() > deadline:
                raise LoadUsersError(
                    f"no confirmation letter in the log for {len(missing)} addresses, "
                    f"{sorted(missing)[0]} among them"
                )
            await asyncio.sleep(3)

    async def _read_logs(self) -> list[str]:
        result = await asyncio.to_thread(
            subprocess.run,  # noqa: S603 -- the command is configuration, not input
            self._settings.logs_command.split(),
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.splitlines()

    async def _confirm(self, token: str) -> None:
        async with self._limit:
            response = await self._auth.post("/api/v1/auth/confirm-email", json={"token": token})
        if response.status_code != 200:
            raise LoadUsersError(f"confirm-email: {response.status_code} {response.text}")


async def prepare(settings: LoadUsersSettings) -> int:
    password = read_password(settings.output)
    users = plan_users(settings.users)
    preparation = Preparation(settings, password=password)
    try:
        tokens = await preparation.run(users)
    finally:
        await preparation.close()
    clients = [{"email": user.email, "access_token": tokens[user.email]} for user in users]
    settings.output.parent.mkdir(parents=True, exist_ok=True)
    settings.output.write_text(
        json.dumps({"password": password, "users": clients}, indent=2) + "\n"
    )
    log(f"{len(users)} clients signed in; written to {settings.output}")
    return len(users)


def main() -> int:
    try:
        asyncio.run(prepare(LoadUsersSettings()))
    except (LoadUsersError, httpx.HTTPError, subprocess.CalledProcessError) as error:
        print(f"loadtest users failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
