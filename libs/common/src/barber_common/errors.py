"""Error model of the platform: RFC 9457 ``application/problem+json``.

Every failure leaves a service in the same shape, with a domain ``code`` from
the catalogue in ``docs/04-api-contracts.md`` and the ``correlation_id`` of the
request, so a client report can be traced to a log line without guessing.

A service raises :class:`DomainError` subclasses; turning them into responses
is the job of the ``api`` layer, never of the scenario that raised them.
"""

from __future__ import annotations

import http
from collections.abc import Mapping
from typing import ClassVar

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from barber_common.context import get_correlation_id
from barber_common.logging import get_logger

__all__ = [
    "PROBLEM_CONTENT_TYPE",
    "DomainError",
    "Forbidden",
    "InternalError",
    "NotFound",
    "RateLimited",
    "Unauthorized",
    "ValidationFailed",
    "install_error_handlers",
    "problem_document",
]

PROBLEM_CONTENT_TYPE = "application/problem+json"
ERROR_TYPE_BASE = "https://barber.local/errors/"

_logger = get_logger(__name__)


class DomainError(Exception):
    """Base class of every expected failure.

    Subclasses declare their code and HTTP status as class attributes::

        class SlotAlreadyTaken(DomainError):
            code = "slot_taken"
            http_status = 409
            title = "Slot is already taken"

    ``extra`` carries the fields specific to one error, such as the alternative
    slots offered together with ``slot_taken``.
    """

    code: ClassVar[str] = "internal_error"
    http_status: ClassVar[int] = 500
    title: ClassVar[str] = "Internal error"

    def __init__(
        self,
        detail: str | None = None,
        *,
        extra: Mapping[str, object] | None = None,
    ) -> None:
        self.detail = detail if detail is not None else self.title
        self.extra: dict[str, object] = dict(extra or {})
        super().__init__(self.detail)

    @property
    def type_uri(self) -> str:
        """Stable documentation URI of this error code."""
        return f"{ERROR_TYPE_BASE}{self.code.replace('_', '-')}"


class ValidationFailed(DomainError):
    """Request body, query or headers do not satisfy the contract."""

    code = "validation_error"
    http_status = 422
    title = "Request is not valid"


class Unauthorized(DomainError):
    """No credentials, or credentials that cannot be verified."""

    code = "unauthorized"
    http_status = 401
    title = "Authentication required"


class Forbidden(DomainError):
    """The caller is authenticated but the role does not allow the operation."""

    code = "forbidden_for_role"
    http_status = 403
    title = "Operation is not allowed for this role"


class NotFound(DomainError):
    """The resource does not exist, or is invisible to this caller."""

    code = "not_found"
    http_status = 404
    title = "Resource not found"


class RateLimited(DomainError):
    """The caller exceeded the rate limit of the endpoint."""

    code = "rate_limited"
    http_status = 429
    title = "Too many requests"


class InternalError(DomainError):
    """Anything the service did not expect. Details stay in the log."""

    code = "internal_error"
    http_status = 500
    title = "Internal error"


def problem_document(
    *,
    code: str,
    status: int,
    title: str,
    detail: str,
    instance: str | None = None,
    extra: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Build the response body of RFC 9457.

    The traceback never gets here: an exception message may quote a query, a
    header or a fragment of a password, and the body of a response leaves the
    perimeter.
    """
    document: dict[str, object] = {
        "type": f"{ERROR_TYPE_BASE}{code.replace('_', '-')}",
        "title": title,
        "status": status,
        "code": code,
        "detail": detail,
        "instance": instance,
        "correlation_id": get_correlation_id(),
    }
    document.update(extra or {})
    return document


def _problem_response(status: int, document: Mapping[str, object]) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content=dict(document),
        media_type=PROBLEM_CONTENT_TYPE,
    )


def _log_problem(request: Request, status: int, code: str, detail: str) -> None:
    """Log a failure at the level its status deserves.

    An expected client error at ``ERROR`` makes the error-rate alert fire during
    normal operation, so only ``5xx`` is an error here.
    """
    fields = {
        "http_method": request.method,
        "http_path": request.url.path,
        "http_status": status,
        "error_code": code,
    }
    if status >= 500:
        _logger.error(detail, exc_info=True, **fields)
    else:
        _logger.warning(detail, **fields)


async def _domain_error_handler(request: Request, exc: Exception) -> Response:
    if not isinstance(exc, DomainError):  # pragma: no cover - guarded by the router
        raise exc

    _log_problem(request, exc.http_status, exc.code, exc.detail)
    return _problem_response(
        exc.http_status,
        problem_document(
            code=exc.code,
            status=exc.http_status,
            title=exc.title,
            detail=exc.detail,
            instance=request.url.path,
            extra=exc.extra,
        ),
    )


async def _validation_error_handler(request: Request, exc: Exception) -> Response:
    if not isinstance(exc, RequestValidationError):  # pragma: no cover
        raise exc

    violations: list[dict[str, object]] = [
        {
            "location": ".".join(str(part) for part in problem["loc"]),
            "message": problem["msg"],
            "type": problem["type"],
        }
        for problem in exc.errors()
    ]
    detail = "Request does not match the contract"

    _log_problem(request, ValidationFailed.http_status, ValidationFailed.code, detail)
    return _problem_response(
        ValidationFailed.http_status,
        problem_document(
            code=ValidationFailed.code,
            status=ValidationFailed.http_status,
            title=ValidationFailed.title,
            detail=detail,
            instance=request.url.path,
            extra={"violations": violations},
        ),
    )


_STATUS_CODES: dict[int, str] = {
    401: Unauthorized.code,
    403: Forbidden.code,
    404: NotFound.code,
    422: ValidationFailed.code,
    429: RateLimited.code,
    500: InternalError.code,
}


async def _http_exception_handler(request: Request, exc: Exception) -> Response:
    """Translate the exceptions raised by Starlette itself, such as 404 and 405."""
    if not isinstance(exc, StarletteHTTPException):  # pragma: no cover
        raise exc

    status = exc.status_code
    code = _STATUS_CODES.get(status, "http_error")
    title = http.HTTPStatus(status).phrase
    detail = str(exc.detail) if exc.detail else title

    _log_problem(request, status, code, detail)
    return _problem_response(
        status,
        problem_document(
            code=code,
            status=status,
            title=title,
            detail=detail,
            instance=request.url.path,
        ),
    )


async def _unhandled_error_handler(request: Request, exc: Exception) -> Response:
    """Last resort: the traceback goes to the log, the client gets a code."""
    _logger.error(
        "unhandled exception",
        exc_info=exc,
        http_method=request.method,
        http_path=request.url.path,
        http_status=InternalError.http_status,
        error_code=InternalError.code,
    )
    return _problem_response(
        InternalError.http_status,
        problem_document(
            code=InternalError.code,
            status=InternalError.http_status,
            title=InternalError.title,
            detail="The service failed to process the request",
            instance=request.url.path,
        ),
    )


def install_error_handlers(app: FastAPI) -> None:
    """Register the handlers that keep every failure in the same format."""
    app.add_exception_handler(DomainError, _domain_error_handler)
    app.add_exception_handler(RequestValidationError, _validation_error_handler)
    app.add_exception_handler(StarletteHTTPException, _http_exception_handler)
    app.add_exception_handler(Exception, _unhandled_error_handler)
