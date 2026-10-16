"""Every failure leaves a service as RFC 9457, with a domain code."""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from sqlalchemy.exc import DBAPIError
from sqlalchemy.exc import TimeoutError as PoolTimeout

from barber_common.context import bind_context
from barber_common.errors import (
    PROBLEM_CONTENT_TYPE,
    DomainError,
    RateLimited,
    document_problem_responses,
    install_error_handlers,
)

SECRET_IN_THE_TRACEBACK = "connection to host db-1 refused for user booking"


class SlotAlreadyTaken(DomainError):
    """Domain error of the booking service, used here as a representative case."""

    code = "slot_taken"
    http_status = 409
    title = "Slot is already taken"


class DriverError(Exception):
    """What the asyncpg dialect raises: an error that carries a SQLSTATE."""

    def __init__(self, sqlstate: str) -> None:
        super().__init__(f"SQLSTATE {sqlstate}")
        self.sqlstate = sqlstate


class Booking(BaseModel):
    master_id: str
    duration_min: int


def build_app() -> FastAPI:
    app = FastAPI()
    install_error_handlers(app)

    @app.get("/taken")
    async def taken() -> None:
        raise SlotAlreadyTaken(
            "The master is busy from 15:00 to 15:45",
            extra={"alternatives": ["2026-09-05T15:45:00Z"]},
        )

    @app.get("/limited")
    async def limited() -> None:
        raise RateLimited("Slow down", headers={"Retry-After": "12"})

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError(SECRET_IN_THE_TRACEBACK)

    @app.get("/missing")
    async def missing() -> None:
        raise HTTPException(status_code=404, detail="Booking not found")

    @app.get("/database/{sqlstate}")
    async def database(sqlstate: str) -> None:
        raise DBAPIError("UPDATE salons SET name = $1", ("a\x00b",), DriverError(sqlstate))

    @app.get("/saturated")
    async def saturated() -> None:
        raise PoolTimeout("QueuePool limit of size 10 overflow 5 reached")

    @app.post("/bookings")
    async def create(booking: Booking) -> dict[str, str]:
        return {"master_id": booking.master_id}

    return app


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=build_app(), raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def test_domain_error_becomes_a_problem_document(client: httpx.AsyncClient) -> None:
    response = await client.get("/taken")

    body = response.json()

    assert response.status_code == 409
    assert response.headers["content-type"].startswith(PROBLEM_CONTENT_TYPE)
    assert body["type"] == "https://barber.local/errors/slot-taken"
    assert body["title"] == "Slot is already taken"
    assert body["status"] == 409
    assert body["code"] == "slot_taken"
    assert body["detail"] == "The master is busy from 15:00 to 15:45"
    assert body["instance"] == "/taken"


async def test_domain_error_carries_its_own_extra_fields(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get("/taken")

    assert response.json()["alternatives"] == ["2026-09-05T15:45:00Z"]


async def test_domain_error_carries_its_headers_to_the_response(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get("/limited")

    assert response.status_code == 429
    assert response.headers["retry-after"] == "12"
    assert response.json()["code"] == "rate_limited"


async def test_correlation_id_of_the_request_reaches_the_response(
    client: httpx.AsyncClient,
) -> None:
    with bind_context(correlation_id="c-8f2a"):
        response = await client.get("/taken")

    assert response.json()["correlation_id"] == "c-8f2a"


async def test_correlation_id_is_null_when_no_context_is_bound(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get("/taken")

    assert response.json()["correlation_id"] is None


async def test_unexpected_exception_becomes_500_with_a_domain_code(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get("/boom")

    assert response.status_code == 500
    assert response.json()["code"] == "internal_error"


async def test_unexpected_exception_does_not_leak_the_traceback(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get("/boom")

    assert SECRET_IN_THE_TRACEBACK not in response.text
    assert "Traceback" not in response.text
    assert "RuntimeError" not in response.text


async def test_invalid_request_body_becomes_422_in_the_same_format(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post("/bookings", json={"master_id": "m-1"})

    assert response.status_code == 422
    assert response.headers["content-type"].startswith(PROBLEM_CONTENT_TYPE)
    assert response.json()["code"] == "validation_error"


async def test_invalid_request_body_names_the_offending_field(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post("/bookings", json={"master_id": "m-1"})

    violations = response.json()["violations"]

    assert [violation["location"] for violation in violations] == ["body.duration_min"]


async def test_starlette_http_exception_becomes_a_problem_document(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get("/missing")

    assert response.status_code == 404
    assert response.headers["content-type"].startswith(PROBLEM_CONTENT_TYPE)
    assert response.json()["code"] == "not_found"


async def test_unknown_route_becomes_a_problem_document(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get("/there-is-no-such-route")

    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


async def test_the_openapi_document_describes_errors_as_they_are_sent() -> None:
    app = build_app()
    document_problem_responses(app)

    document = app.openapi()

    paths = document["paths"]
    invalid = paths["/bookings"]["post"]["responses"]["422"]
    assert set(invalid["content"]) == {PROBLEM_CONTENT_TYPE}
    assert invalid["content"][PROBLEM_CONTENT_TYPE]["schema"] == {
        "$ref": "#/components/schemas/Problem"
    }
    schemas = document["components"]["schemas"]
    assert "Problem" in schemas
    assert "HTTPValidationError" not in schemas
    assert "ValidationError" not in schemas


async def test_an_undecodable_body_is_a_documented_400(client: httpx.AsyncClient) -> None:
    app = build_app()
    document_problem_responses(app)

    response = await client.post(
        "/bookings", content=b"\x80\x81", headers={"Content-Type": "application/json"}
    )
    responses = app.openapi()["paths"]["/bookings"]["post"]["responses"]

    assert response.status_code == 400
    assert response.headers["content-type"].startswith(PROBLEM_CONTENT_TYPE)
    assert set(responses["400"]["content"]) == {PROBLEM_CONTENT_TYPE}


def test_an_operation_without_a_body_documents_no_400() -> None:
    app = build_app()
    document_problem_responses(app)

    responses = app.openapi()["paths"]["/taken"]["get"]["responses"]

    assert "400" not in responses


async def test_text_the_database_cannot_store_is_the_clients_mistake(
    client: httpx.AsyncClient,
) -> None:
    """Found by the fuzzer: a NUL in a string reached PostgreSQL and answered 500."""
    response = await client.get("/database/22021")

    assert response.status_code == 422
    assert response.headers["content-type"].startswith(PROBLEM_CONTENT_TYPE)
    assert response.json()["code"] == "validation_error"


async def test_any_other_database_error_stays_internal(client: httpx.AsyncClient) -> None:
    response = await client.get("/database/08006")

    assert response.status_code == 500
    assert response.json()["code"] == "internal_error"


async def test_an_exhausted_pool_is_an_overload_and_not_a_bug(client: httpx.AsyncClient) -> None:
    """Found under three times the peak load: a saturated pool answered 500."""
    response = await client.get("/saturated")

    assert response.status_code == 503
    assert response.headers["retry-after"] == "1"
    assert response.json()["code"] == "service_overloaded"
