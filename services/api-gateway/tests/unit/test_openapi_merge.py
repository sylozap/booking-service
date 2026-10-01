"""Putting the OpenAPI documents of the services together."""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import pytest

from barber_gateway import openapi
from barber_gateway.openapi import OpenApiConflict

GATEWAY: dict[str, object] = {"openapi": "3.1.0", "paths": {}}


def operation(
    *, tags: list[str] | None = None, responses: dict[str, object] | None = None
) -> dict[str, object]:
    return {
        "operationId": f"op_{uuid4().hex}",
        "description": "Does something.",
        "tags": tags or [],
        "responses": {"200": {"description": "OK"}, **(responses or {})},
    }


def ref(name: str) -> dict[str, str]:
    return {"$ref": f"#/components/schemas/{name}"}


def document(
    paths: dict[str, object], schemas: dict[str, object] | None = None
) -> dict[str, object]:
    return {"openapi": "3.1.0", "paths": paths, "components": {"schemas": schemas or {}}}


def merge_documents(*, services: dict[str, dict[str, object]]) -> dict[str, Any]:
    """The merged document as plain JSON, for indexing into without casts."""
    merged = openapi.merge_documents(gateway=GATEWAY, services=services)
    plain: dict[str, Any] = json.loads(json.dumps(merged))
    return plain


def test_the_paths_of_every_service_end_up_in_one_document() -> None:
    merged = merge_documents(
        services={
            "catalog": document({"/api/v1/salons": {"get": operation()}}),
            "booking": document({"/api/v1/bookings": {"post": operation()}}),
        },
    )

    assert set(merged["paths"]) == {"/api/v1/salons", "/api/v1/bookings"}


def test_internal_endpoints_are_left_out() -> None:
    merged = merge_documents(
        services={"auth": document({"/internal/v1/token": {"post": operation()}})},
    )

    assert merged["paths"] == {}


def test_two_services_describing_one_operation_is_a_conflict() -> None:
    with pytest.raises(OpenApiConflict, match="GET /api/v1/masters/"):
        merge_documents(
            services={
                "catalog": document({"/api/v1/masters/{master_id}": {"get": operation()}}),
                "booking": document({"/api/v1/masters/{master_id}": {"get": operation()}}),
            },
        )


def test_two_services_may_share_a_path_with_different_methods() -> None:
    merged = merge_documents(
        services={
            "catalog": document({"/api/v1/things": {"get": operation()}}),
            "booking": document({"/api/v1/things": {"post": operation()}}),
        },
    )

    assert set(merged["paths"]["/api/v1/things"]) == {"get", "post"}


def test_tags_name_the_service_that_owns_the_endpoint() -> None:
    merged = merge_documents(
        services={
            "catalog": document({"/api/v1/salons": {"get": operation(tags=["salons"])}}),
            "booking": document({"/api/v1/bookings": {"get": operation()}}),
        },
    )

    paths = merged["paths"]
    assert paths["/api/v1/salons"]["get"]["tags"] == ["catalog · salons"]
    assert paths["/api/v1/bookings"]["get"]["tags"] == ["booking"]
    assert merged["tags"] == [{"name": "booking"}, {"name": "catalog · salons"}]


def test_a_schema_shared_by_two_services_is_kept_once() -> None:
    page = {"type": "object", "properties": {"next_cursor": {"type": "string"}}}

    merged = merge_documents(
        services={
            "catalog": document({"/api/v1/a": {"get": operation()}}, {"Page": page}),
            "booking": document({"/api/v1/b": {"get": operation()}}, {"Page": page}),
        },
    )

    schemas = merged["components"]["schemas"]
    assert schemas["Page"] == page
    assert "booking.Page" not in schemas


def test_schemas_of_one_name_and_two_meanings_are_both_kept_apart() -> None:
    catalog_item = {"type": "object", "properties": {"name": {"type": "string"}}}
    booking_item = {"type": "object", "properties": {"start_at": {"type": "string"}}}
    page = {"type": "object", "properties": {"items": {"type": "array", "items": ref("Item")}}}

    merged = merge_documents(
        services={
            "catalog": document(
                {"/api/v1/a": {"get": operation()}}, {"Item": catalog_item, "Page": page}
            ),
            "booking": document(
                {"/api/v1/b": {"get": operation(responses={"201": {"content": ref("Page")}})}},
                {"Item": booking_item, "Page": page},
            ),
        },
    )

    schemas = merged["components"]["schemas"]
    assert schemas["Item"] == catalog_item
    assert schemas["booking.Item"] == booking_item
    # Page is the same text in both, but booking's means a page of its own items.
    assert schemas["booking.Page"]["properties"]["items"]["items"] == ref("booking.Item")
    created = merged["paths"]["/api/v1/b"]["get"]["responses"]["201"]
    assert created["content"] == ref("booking.Page")


def test_the_answers_of_the_gateway_are_added_to_every_operation() -> None:
    merged = merge_documents(
        services={"booking": document({"/api/v1/bookings": {"post": operation()}})},
    )

    responses = merged["paths"]["/api/v1/bookings"]["post"]["responses"]
    assert {"401", "429", "503", "504"} <= set(responses)
    assert "Retry-After" in responses["429"]["headers"]
    assert responses["504"]["content"]["application/problem+json"]["schema"] == ref("Problem")


def test_a_public_operation_is_not_said_to_answer_401() -> None:
    merged = merge_documents(
        services={"catalog": document({"/api/v1/salons": {"get": operation()}})},
    )

    assert "401" not in merged["paths"]["/api/v1/salons"]["get"]["responses"]


def test_an_answer_the_service_documents_itself_is_kept() -> None:
    own = {"description": "The catalog is not answering"}

    merged = merge_documents(
        services={
            "booking": document(
                {"/api/v1/availability": {"get": operation(responses={"503": own})}}
            )
        },
    )

    responses = merged["paths"]["/api/v1/availability"]["get"]["responses"]
    assert responses["503"] == own


def test_the_problem_schema_is_always_there() -> None:
    merged = merge_documents(services={})

    assert "Problem" in merged["components"]["schemas"]
