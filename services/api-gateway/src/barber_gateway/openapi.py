"""One OpenAPI document for the whole platform, served by the gateway.

The services describe themselves; the gateway puts the descriptions together
and adds what only it knows.

* **Paths** are taken as the services publish them -- they already carry
  ``/api/v1`` -- minus ``/internal/*``, which is not reachable through the
  gateway. The same method on the same path in two services is a conflict and
  fails the merge rather than letting one of them win quietly.
* **Tags** are prefixed with the service, ``catalog · masters``, so the
  endpoints group by owner.
* **Schemas** of the same name are kept once when they are the same, and
  renamed to ``<service>.<Name>`` when they differ, with every ``$ref`` of that
  service following the rename. Most shared names come from the chassis or the
  contracts and are the same.
* **Responses the gateway itself produces** are added to every operation
  that does not declare them already: ``429`` (with ``Retry-After``), ``503``
  and ``504`` everywhere, ``401`` wherever the gateway demands a token. They
  are the gateway's to document, because a service never sends them on its own
  behalf in a way the gateway passes through unchanged.

The document is collected on the first request for it and cached, not built
at startup: the gateway may start before the services it describes, and a
document missing a service is worse than one collected a minute later. A
document that came out incomplete is served -- it says which services are
missing -- but not cached, so the next request tries again.
"""

from __future__ import annotations

import asyncio
import copy
import time
from collections.abc import Callable, Mapping

from barber_common.errors import (
    PROBLEM_SCHEMA_NAME,
    DomainError,
    Problem,
    problem_response,
)
from barber_common.logging import get_logger
from barber_gateway.auth import is_public
from barber_gateway.clients.openapi import OpenApiClient

__all__ = ["GATEWAY_SOURCE", "OpenApiConflict", "PlatformDocument", "merge_documents"]

_logger = get_logger(__name__)

GATEWAY_SOURCE = "api-gateway"

_HTTP_METHODS = frozenset({"get", "put", "post", "delete", "options", "head", "patch", "trace"})
_SCHEMA_REF = "#/components/schemas/"

JsonObject = dict[str, object]


class OpenApiConflict(Exception):
    """Two services describe the same operation, or the same scheme differently."""


def merge_documents(
    *,
    gateway: JsonObject,
    services: Mapping[str, JsonObject],
    title: str = "Barber Platform API",
    version: str = "1",
) -> JsonObject:
    """Put the documents of the gateway and of the services together."""
    paths: dict[str, JsonObject] = {}
    schemas: JsonObject = {PROBLEM_SCHEMA_NAME: Problem.model_json_schema()}
    security_schemes: JsonObject = {}
    tags: set[str] = set()
    operation_ids: set[str] = set()

    for source, document in [(GATEWAY_SOURCE, gateway), *services.items()]:
        incoming = _object(_object(document.get("components")).get("schemas"))
        renames = _schema_renames(source, incoming, schemas)
        renamed = _object(_rewrite_refs(document, renames))

        for name, schema in _object(_object(renamed.get("components")).get("schemas")).items():
            schemas.setdefault(renames.get(name, name), schema)

        for name, scheme in _object(
            _object(renamed.get("components")).get("securitySchemes")
        ).items():
            if security_schemes.setdefault(name, scheme) != scheme:
                raise OpenApiConflict(f"security scheme {name} differs in {source}")

        for path, item in _object(renamed.get("paths")).items():
            if path.startswith("/internal/") or not isinstance(item, dict):
                continue
            merged_item = paths.setdefault(path, {})
            for method, operation in item.items():
                if method not in _HTTP_METHODS:
                    merged_item.setdefault(method, operation)
                    continue
                if method in merged_item:
                    raise OpenApiConflict(f"{method.upper()} {path} is described twice")
                if not isinstance(operation, dict):
                    continue
                merged_item[method] = _operation(source, method, path, operation, tags)
                operation_id = operation.get("operationId")
                if isinstance(operation_id, str):
                    if operation_id in operation_ids:
                        raise OpenApiConflict(f"operationId {operation_id} is used twice")
                    operation_ids.add(operation_id)

    return {
        "openapi": "3.1.0",
        "info": {
            "title": title,
            "version": version,
            "description": (
                "Every endpoint of the platform, through the gateway. "
                "Errors are `application/problem+json` with a domain `code`."
            ),
        },
        "paths": paths,
        "components": {"schemas": schemas, "securitySchemes": security_schemes},
        "tags": [{"name": tag} for tag in sorted(tags)],
    }


def _operation(
    source: str, method: str, path: str, operation: JsonObject, tags: set[str]
) -> JsonObject:
    """One operation, with its tags prefixed and the gateway's answers added."""
    result = dict(operation)
    own_tags = [tag for tag in _list(operation.get("tags")) if isinstance(tag, str)]
    prefixed = [f"{source} · {tag}" for tag in own_tags] or [source]
    result["tags"] = prefixed
    tags.update(prefixed)

    responses = dict(_object(operation.get("responses")))
    if not is_public(method, path):
        responses.setdefault("401", problem_response("No valid access token"))
    responses.setdefault("429", _rate_limited())
    responses.setdefault("503", problem_response("The service is not reachable"))
    responses.setdefault("504", problem_response("The service did not answer in time"))
    result["responses"] = responses
    return result


def _rate_limited() -> JsonObject:
    response = problem_response("Too many requests; the limit depends on the caller")
    response["headers"] = {
        "Retry-After": {
            "description": "Seconds until one more request fits under the limit.",
            "schema": {"type": "integer", "minimum": 1},
        }
    }
    return response


def _schema_renames(source: str, incoming: JsonObject, existing: JsonObject) -> dict[str, str]:
    """Which schemas of ``source`` clash with ones already merged.

    Repeated until nothing changes: a schema identical in text can still mean
    something else if a schema it refers to was renamed.
    """
    renames: dict[str, str] = {}
    changed = True
    while changed:
        changed = False
        for name, schema in incoming.items():
            if name in renames or name not in existing:
                continue
            if _rewrite_refs(schema, renames) != existing[name]:
                renames[name] = f"{source}.{name}"
                changed = True
    return renames


def _rewrite_refs(value: object, renames: Mapping[str, str]) -> object:
    """A copy of a JSON value with its schema references renamed."""
    if not renames:
        return copy.deepcopy(value)
    if isinstance(value, dict):
        rewritten: JsonObject = {}
        for key, item in value.items():
            if key == "$ref" and isinstance(item, str) and item.startswith(_SCHEMA_REF):
                name = item.removeprefix(_SCHEMA_REF)
                rewritten[key] = _SCHEMA_REF + renames.get(name, name)
            else:
                rewritten[key] = _rewrite_refs(item, renames)
        return rewritten
    if isinstance(value, list):
        return [_rewrite_refs(item, renames) for item in value]
    return value


def _object(value: object) -> JsonObject:
    return value if isinstance(value, dict) else {}


def _list(value: object) -> list[object]:
    return value if isinstance(value, list) else []


class PlatformDocument:
    """The merged document, collected on demand and cached while complete."""

    def __init__(
        self,
        *,
        gateway: Callable[[], JsonObject],
        documents: OpenApiClient,
        ttl_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._gateway = gateway
        self._documents = documents
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._cached: JsonObject | None = None
        self._cached_at = 0.0
        # One collection at a time: a burst of requests for the document
        # right after a deploy costs each service one call, not one each.
        self._lock = asyncio.Lock()

    async def get(self) -> JsonObject:
        """The document of the platform, collected now if the cache has none."""
        cached = self._fresh()
        if cached is not None:
            return cached

        async with self._lock:
            cached = self._fresh()
            if cached is not None:
                return cached
            return await self._collect()

    def _fresh(self) -> JsonObject | None:
        if self._cached is None or self._clock() - self._cached_at >= self._ttl_seconds:
            return None
        return self._cached

    async def _collect(self) -> JsonObject:
        upstreams = self._documents.upstreams
        answers = await asyncio.gather(
            *(self._documents.fetch(upstream) for upstream in upstreams),
            return_exceptions=True,
        )

        collected: dict[str, JsonObject] = {}
        missing: list[str] = []
        for upstream, answer in zip(upstreams, answers, strict=True):
            if isinstance(answer, dict):
                collected[upstream.value] = answer
            elif isinstance(answer, DomainError):
                missing.append(upstream.value)
            else:
                raise answer

        document = merge_documents(gateway=self._gateway(), services=collected)
        if missing:
            # Served, so /docs still shows what there is; not cached, so the
            # next request asks the missing services again.
            _logger.warning("openapi document is incomplete", missing=missing)
            info = _object(document.get("info"))
            info["x-missing-services"] = missing
            return document

        self._cached = document
        self._cached_at = self._clock()
        return document
