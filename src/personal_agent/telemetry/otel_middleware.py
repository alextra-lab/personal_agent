"""Request-boundary root span middleware (ADR-0129 D4).

Opens exactly one root span per served HTTP request. This is deliberately
generic — it does not attempt to read ``session_id`` off the request, since
where that arrives varies by endpoint (query param, form field, path param,
WebSocket). Log records emitted while the span is active pick up its
trace/span identity via :func:`personal_agent.telemetry.logger._add_span_context`;
``session_id``, when known, continues to reach those same records through the
existing ``structlog.contextvars`` binding (ADR-0107 D5) — unchanged by this
middleware.
"""

from collections.abc import Awaitable, Callable

from opentelemetry import trace
from opentelemetry.trace import Tracer
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp


class RequestRootSpanMiddleware(BaseHTTPMiddleware):
    """Open a root span for the lifetime of each served HTTP request."""

    def __init__(self, app: ASGIApp, tracer: Tracer | None = None) -> None:
        """Initialize the middleware.

        Args:
            app: The wrapped ASGI application.
            tracer: Tracer to open spans with. Defaults to the process-wide
                tracer (via :func:`opentelemetry.trace.get_tracer`) resolved
                against whatever provider :func:`configure_tracing` installed
                at startup. Tests inject their own tracer, bound to a provider
                carrying an in-memory exporter, for isolation.
        """
        super().__init__(app)
        self._tracer = tracer if tracer is not None else trace.get_tracer(__name__)

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        """Open a root span wrapping the request, then dispatch to the app.

        Args:
            request: The incoming request.
            call_next: Continues the middleware chain / dispatches to the route.

        Returns:
            The response produced downstream.
        """
        with self._tracer.start_as_current_span(f"{request.method} {request.url.path}") as span:
            span.set_attribute("http.method", request.method)
            span.set_attribute("http.target", request.url.path)
            response = await call_next(request)
            span.set_attribute("http.status_code", response.status_code)
            return response
