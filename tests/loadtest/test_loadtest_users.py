"""The preparation of the load test clients, against a fake auth and a fake log."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from loadtest_users import (
    LoadUser,
    LoadUsersError,
    LoadUsersSettings,
    Preparation,
    confirmation_tokens,
    plan_users,
    read_password,
)


def letter(address: str, token: str, *, logger: str = "barber_notification.messages") -> str:
    return json.dumps(
        {
            "logger": logger,
            "event": "message",
            "template": "email_confirmation",
            "address": address,
            "body": f"Open http://localhost:8080/confirm-email?token={token} to confirm.",
        }
    )


def test_users_are_the_same_on_every_run() -> None:
    assert plan_users(2) == [
        LoadUser(email="load-001@load.barber.example", phone="+79993000001"),
        LoadUser(email="load-002@load.barber.example", phone="+79993000002"),
    ]
    assert len({user.phone for user in plan_users(100)}) == 100


def test_tokens_are_read_from_the_letters_of_the_log() -> None:
    lines = [
        "not json at all",
        json.dumps(["a", "list"]),
        json.dumps({"logger": "uvicorn.access", "event": "GET /health"}),
        letter("a@x.example", "first-token-aaaaaaaa"),
        letter("b@x.example", "token-of-b-bbbbbbbbb", logger="barber_notification.worker"),
        letter("a@x.example", "second-token-aaaaaaa"),
    ]

    assert confirmation_tokens(lines) == {"a@x.example": "second-token-aaaaaaa"}


def test_the_password_of_an_earlier_run_is_reused(tmp_path: Path) -> None:
    output = tmp_path / "users.json"

    fresh = read_password(output)
    output.write_text(json.dumps({"password": "kept-password", "users": []}))

    assert len(fresh) >= 16
    assert read_password(output) == "kept-password"


class FakeAuth:
    """Accounts that need a confirmed address to sign in."""

    def __init__(self, password: str) -> None:
        self.password = password
        self.registered: set[str] = set()
        self.confirmed: set[str] = set()

    def token_of(self, email: str) -> str:
        return f"token-{email.split('@')[0]}-0000000000"

    def handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if request.url.path == "/api/v1/auth/register":
            if body["email"] in self.registered:
                return httpx.Response(409, json={"code": "user_exists"})
            self.registered.add(body["email"])
            return httpx.Response(201, json={"user_id": "x"})
        if request.url.path == "/api/v1/auth/confirm-email":
            [email] = [e for e in self.registered if self.token_of(e) == body["token"]]
            self.confirmed.add(email)
            return httpx.Response(200, json={"email": email})
        if request.url.path == "/api/v1/auth/login":
            if body["password"] != self.password:
                return httpx.Response(401, json={"code": "invalid_credentials"})
            if body["email"] not in self.confirmed:
                return httpx.Response(403, json={"code": "email_not_confirmed"})
            return httpx.Response(200, json={"access_token": f"token-of-{body['email']}"})
        return httpx.Response(404)


def preparation(auth: FakeAuth, password: str, **settings: object) -> Preparation:
    prepared = Preparation(
        LoadUsersSettings(**settings),  # type: ignore[arg-type]
        password=password,
        transport=httpx.MockTransport(auth.handle),
    )

    async def logs() -> list[str]:
        return [letter(email, auth.token_of(email)) for email in sorted(auth.registered)]

    prepared._read_logs = logs  # type: ignore[method-assign]
    return prepared


async def test_new_accounts_are_registered_confirmed_and_signed_in() -> None:
    auth = FakeAuth("secret-password")
    users = plan_users(3)
    prepared = preparation(auth, "secret-password")

    tokens = await prepared.run(users)
    await prepared.close()

    assert auth.confirmed == {user.email for user in users}
    assert tokens == {user.email: f"token-of-{user.email}" for user in users}


async def test_a_second_run_only_signs_in() -> None:
    auth = FakeAuth("secret-password")
    users = plan_users(2)
    await preparation(auth, "secret-password").run(users)

    second = preparation(auth, "secret-password")
    second._confirm = None  # type: ignore[assignment,method-assign]
    tokens = await second.run(users)

    assert tokens.keys() == {user.email for user in users}


async def test_an_account_with_another_password_stops_the_run() -> None:
    auth = FakeAuth("the-first-password")
    await preparation(auth, "the-first-password").run(plan_users(1))

    with pytest.raises(LoadUsersError, match="registered with another one"):
        await preparation(auth, "a-new-password").run(plan_users(1))


async def test_a_letter_that_never_comes_stops_the_run() -> None:
    auth = FakeAuth("secret-password")
    prepared = preparation(auth, "secret-password", confirmation_timeout_seconds=0)

    async def no_letters() -> list[str]:
        return []

    prepared._read_logs = no_letters  # type: ignore[method-assign]

    with pytest.raises(LoadUsersError, match="no confirmation letter"):
        await prepared.run(plan_users(1))
