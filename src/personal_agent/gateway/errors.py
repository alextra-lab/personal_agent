"""Standard error response shapes for the Seshat API Gateway.

All gateway error responses follow the schema::

    {
        "error": "<error_code>",
        "message": "<human-readable description>",
        "status": <HTTP status code>
    }
"""

from typing import Any

from fastapi import HTTPException
from fastapi.responses import JSONResponse


def gateway_error(status: int, error: str, message: str) -> JSONResponse:
    """Build a consistent JSON error response.

    Args:
        status: HTTP status code (e.g. 401, 403, 404).
        error: Machine-readable error code (e.g. ``"unauthorized"``).
        message: Human-readable description.

    Returns:
        JSONResponse with the error payload and the given status code.
    """
    return JSONResponse(
        status_code=status,
        content={"error": error, "message": message, "status": status},
    )


def not_found(resource: str = "resource") -> HTTPException:
    """Return a 404 HTTPException with a standardised payload.

    Args:
        resource: Name of the missing resource (used in message).

    Returns:
        HTTPException(404) with detail dict.
    """
    return HTTPException(
        status_code=404,
        detail={"error": "not_found", "message": f"{resource} not found", "status": 404},
    )


def service_unavailable(message: str = "Backend service is unavailable") -> HTTPException:
    """Return a 503 HTTPException.

    Args:
        message: Human-readable description of why the service is unavailable.

    Returns:
        HTTPException(503) with detail dict.
    """
    return HTTPException(
        status_code=503,
        detail={"error": "service_unavailable", "message": message, "status": 503},
    )


def add_error_handlers(app: Any) -> None:
    """Register gateway exception handlers on a FastAPI app.

    Args:
        app: FastAPI application instance.
    """
    from fastapi import Request

    async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
        detail: Any = exc.detail
        content: dict[str, Any]
        # detail may be a dict (our structured errors) or a plain string
        if isinstance(detail, dict):
            content = detail
        else:
            content = {"error": "error", "message": str(detail), "status": exc.status_code}
        return JSONResponse(status_code=exc.status_code, content=content)

    app.add_exception_handler(HTTPException, http_exception_handler)
