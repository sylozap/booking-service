"""Forwarding a request to the service behind the gateway, untouched.

The gateway does not read what it carries. The body of a request goes to the
service as it arrives, and the body of the answer comes back chunk by chunk as
the service sends it: an answer of a hundred megabytes costs the gateway one
chunk of memory, not a hundred megabytes, and the first byte reaches the client
before the last one has left the service. ``aiter_raw`` rather than
``aiter_bytes`` -- a compressed answer stays compressed, so ``Content-Length``
and ``Content-Encoding`` of the service remain true.

Not :class:`~barber_common.http.ServiceClient`, deliberately. That client reads
the answer whole and turns an error status into an exception, which is right
for a caller that wants a contract and wrong for a proxy that must hand back
exactly what the service said (``docs/CODING_STANDARDS.md`` §8 names the
exception). What is kept from it is the circuit breaker, one per service.

Nothing is retried. A ``POST`` that timed out may have created a booking, and
only the client knows whether sending it again is what it wants.

Failures before the answer starts are the platform's own ``problem+json``:

* the service took the request and did not answer in time -- ``504
  upstream_timeout``;
* it could not be reached, or its breaker is open -- ``503
  upstream_unavailable``.

A failure after the answer started cannot change the status any more: the
connection is cut, and the client sees a truncated body.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping

import httpx
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse

from barber_common.context import get_correlation_id
from barber_common.http import CircuitBreaker, Timeouts, UpstreamTimeout, UpstreamUnavailable
from barber_common.logging import get_logger
from barber_common.middleware import CORRELATION_ID_HEADER
from barber_gateway.routing import Upstream

__all__ = ["HOP_BY_HOP_HEADERS", "IDENTITY_HEADERS", "Proxy"]

_logger = get_logger(__name__)

# RFC 9110 §7.6.1: these describe one connection, not the message, and a
# proxy does not pass them on. "trailers" is the spelling of the obsolete
# RFC 2616 list, kept because clients still send it.
HOP_BY_HOP_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "proxy-connection",
        "te",
        "trailer",
        "trailers",
        "transfer-encoding",
        "upgrade",
    }
)

# Who the caller is, according to the gateway. Never taken from the client:
# whatever a client sends under these names is dropped, and the gateway writes
# its own from the verified token. Trusting the client's would let anyone call
# as anyone.
IDENTITY_HEADERS = frozenset({"x-user-id", "x-roles"})

# Set by the gateway itself rather than passed along.
_REWRITTEN_REQUEST_HEADERS = frozenset(
    {
        "host",
        CORRELATION_ID_HEADER,
        "x-forwarded-for",
        "x-forwarded-host",
        "x-forwarded-proto",
    }
)

# The gateway's own server writes these on every answer, and the correlation
# middleware writes the id. Passing the service's copies on would send each of
# them twice.
_REWRITTEN_RESPONSE_HEADERS = frozenset({"date", "server", CORRELATION_ID_HEADER})


class Proxy:
    """A connection pool and a circuit breaker per service behind the gateway."""

    def __init__(
        self,
        *,
        base_urls: Mapping[Upstream, str],
        timeouts: Timeouts,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._clients = {
            upstream: httpx.AsyncClient(
                base_url=base_url,
                timeout=timeouts.to_httpx(),
                transport=transport,
                # A redirect is an answer for the client to follow, not for the
                # gateway to follow on its behalf.
                follow_redirects=False,
            )
            for upstream, base_url in base_urls.items()
        }
        self._breakers = {upstream: CircuitBreaker(name=upstream) for upstream in base_urls}

    async def aclose(self) -> None:
        """Release every connection pool."""
        for client in self._clients.values():
            await client.aclose()

    async def forward(
        self,
        request: Request,
        upstream: Upstream,
        *,
        identity: Mapping[str, str],
    ) -> Response:
        """Send the request to the service and stream its answer back.

        ``identity`` holds the identity headers the gateway vouches for; the
        ones the client sent are dropped whether or not it has any.
        """
        breaker = self._breakers[upstream]
        breaker.check()

        client = self._clients[upstream]
        outgoing = client.build_request(
            request.method,
            _target(request),
            headers=_request_headers(request, identity),
            content=_request_body(request),
        )

        try:
            answer = await client.send(outgoing, stream=True)
        except (httpx.ReadTimeout, httpx.WriteTimeout) as error:
            breaker.record_failure()
            _logger.warning("upstream did not answer in time", upstream=upstream)
            raise UpstreamTimeout(
                f"{upstream} did not answer in time", extra={"upstream": upstream}
            ) from error
        except httpx.TransportError as error:
            # Including ConnectTimeout: a service that does not accept the
            # connection never saw the request, which makes it unavailable
            # rather than slow.
            breaker.record_failure()
            _logger.warning(
                "upstream is unreachable", upstream=upstream, error=type(error).__name__
            )
            raise UpstreamUnavailable(
                f"{upstream} is not reachable", extra={"upstream": upstream}
            ) from error

        # Any answer, a 5xx included, means the service is there. A 503 of
        # booking because catalog is down must not open the breaker of booking.
        breaker.record_success()

        response = StreamingResponse(_relay(answer, upstream), status_code=answer.status_code)
        # Raw pairs rather than a mapping, so a repeated header -- Set-Cookie,
        # Vary -- survives as the service sent it.
        response.raw_headers = _response_headers(answer)
        return response


def _target(request: Request) -> str:
    """The path and query as the client wrote them, escapes included."""
    raw_path = request.scope.get("raw_path") or request.url.path.encode("latin-1")
    query = request.scope.get("query_string", b"")
    target = raw_path.decode("latin-1")
    return f"{target}?{query.decode('latin-1')}" if query else target


def _request_body(request: Request) -> AsyncIterator[bytes] | None:
    """The body as a stream, or nothing at all when the request has none.

    A stream given for a bodiless ``GET`` would go out with
    ``Transfer-Encoding: chunked`` and an empty body, which some servers refuse.
    """
    has_body = "content-length" in request.headers or "transfer-encoding" in request.headers
    return request.stream() if has_body else None


def _request_headers(request: Request, identity: Mapping[str, str]) -> list[tuple[str, str]]:
    """The headers of the client, minus what the gateway owns, plus its own."""
    dropped = (
        HOP_BY_HOP_HEADERS
        | IDENTITY_HEADERS
        | _REWRITTEN_REQUEST_HEADERS
        | _named_by_connection(request.headers.get("connection", ""))
    )
    headers = [
        (name, value) for name, value in request.headers.items() if name.lower() not in dropped
    ]

    client_host = request.client.host if request.client is not None else None
    forwarded_for = request.headers.get("x-forwarded-for")
    if client_host is not None:
        forwarded_for = f"{forwarded_for}, {client_host}" if forwarded_for else client_host
    if forwarded_for:
        headers.append(("x-forwarded-for", forwarded_for))
    headers.append(("x-forwarded-proto", request.url.scheme))
    if host := request.headers.get("host"):
        headers.append(("x-forwarded-host", host))

    correlation_id = get_correlation_id()
    if correlation_id is not None:
        headers.append((CORRELATION_ID_HEADER, correlation_id))

    headers.extend(identity.items())
    return headers


def _response_headers(answer: httpx.Response) -> list[tuple[bytes, bytes]]:
    dropped = (
        HOP_BY_HOP_HEADERS
        | _REWRITTEN_RESPONSE_HEADERS
        | _named_by_connection(answer.headers.get("connection", ""))
    )
    return [
        (name.encode("latin-1"), value.encode("latin-1"))
        for name, value in answer.headers.multi_items()
        if name.lower() not in dropped
    ]


def _named_by_connection(connection: str) -> frozenset[str]:
    """Headers a sender declared hop-by-hop by listing them in ``Connection``."""
    return frozenset(token.strip().lower() for token in connection.split(",") if token.strip())


async def _relay(answer: httpx.Response, upstream: Upstream) -> AsyncIterator[bytes]:
    """The body of the answer, chunk by chunk, as the service sends it.

    The connection goes back to the pool however this ends -- finished, cut by
    the service, or abandoned by a client that went away.
    """
    try:
        async for chunk in answer.aiter_raw():
            yield chunk
    except httpx.HTTPError as error:
        # The status is already on its way; all that is left is to say why
        # the body stops short.
        _logger.warning(
            "upstream broke off its answer", upstream=upstream, error=type(error).__name__
        )
        raise
    finally:
        await answer.aclose()
