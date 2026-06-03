"""Location resolution tool for FRE-230."""

from __future__ import annotations

import re
import time
from dataclasses import asdict, dataclass
from typing import Literal, Protocol, cast

from personal_agent.config import settings
from personal_agent.telemetry import TraceContext, get_logger
from personal_agent.tools.executor import ToolExecutionError
from personal_agent.tools.types import ToolDefinition, ToolParameter

log = get_logger(__name__)

_EXPLICIT_LOCATION_RE = re.compile(
    r"\b(?:i\s+am|i'm|im|i\s+was|currently|today)\s+(?:in|near|at)\s+"
    r"(?P<city>[A-Z][A-Za-zÀ-ÖØ-öø-ÿ' -]{1,80})",
    flags=re.IGNORECASE,
)


@dataclass(frozen=True)
class LocationResolution:
    """Resolved location value object.

    Args:
        city: City or locality name when available.
        country: Country name when available.
        latitude: Device latitude when available.
        longitude: Device longitude when available.
        timezone: Browser-provided IANA timezone name when available.
        source: Source used to resolve the location.
        precise: Whether the result contains full device-fidelity coordinates.
    """

    city: str | None
    country: str | None
    latitude: float | None
    longitude: float | None
    timezone: str | None
    source: Literal["explicit", "client"]
    precise: bool


class LocationProvider(Protocol):
    """Protocol for resolving a location from request context."""

    async def resolve(self, context: TraceContext) -> LocationResolution | None:
        """Resolve a location for a trace.

        Args:
            context: Trace context used for telemetry correlation.

        Returns:
            LocationResolution when a location can be resolved, otherwise None.
        """


class ExplicitLocationProvider:
    """Resolve a city-only location from user-stated session notes.

    This parser intentionally does not geocode the city, infer a country, or
    infer timezone. It only recognizes simple phrases such as "I'm in Lisbon".
    """

    def __init__(self, session_notes: str | None) -> None:
        """Initialize the provider.

        Args:
            session_notes: Session notes or recent user text to scan.
        """
        self._session_notes = session_notes or ""

    async def resolve(self, context: TraceContext) -> LocationResolution | None:
        """Parse an explicit city mention from session notes.

        Args:
            context: Trace context used for telemetry correlation.

        Returns:
            City-level LocationResolution, or None when no location is found.
        """
        match = _EXPLICIT_LOCATION_RE.search(self._session_notes)
        if not match:
            log.info("explicit_location_not_found", trace_id=context.trace_id)
            return None

        city = _clean_city(match.group("city"))
        if not city:
            log.info("explicit_location_not_found", trace_id=context.trace_id)
            return None

        resolution = LocationResolution(
            city=city,
            country=None,
            latitude=None,
            longitude=None,
            timezone=None,
            source="explicit",
            precise=False,
        )
        log.info(
            "explicit_location_resolved",
            trace_id=context.trace_id,
            city=resolution.city,
            country=resolution.country,
            source=resolution.source,
        )
        return resolution


class ClientCoordinatesProvider:
    """Resolve location from browser/device supplied coordinates."""

    def __init__(
        self,
        latitude: float,
        longitude: float,
        timezone: str | None,
        precision: str,
    ) -> None:
        """Initialize the provider.

        Args:
            latitude: Browser/device latitude.
            longitude: Browser/device longitude.
            timezone: Browser-provided IANA timezone string.
            precision: ``"precise"`` keeps coordinates verbatim; ``"coarse"``
                rounds latitude and longitude to two decimals.
        """
        self._latitude = latitude
        self._longitude = longitude
        self._timezone = timezone
        self._precision = precision

    async def resolve(self, context: TraceContext) -> LocationResolution:
        """Return client-provided coordinates.

        Args:
            context: Trace context used for telemetry correlation.

        Returns:
            LocationResolution built from device coordinates.
        """
        coarse = self._precision == "coarse"
        latitude = round(self._latitude, 2) if coarse else self._latitude
        longitude = round(self._longitude, 2) if coarse else self._longitude
        resolution = LocationResolution(
            city=None,
            country=None,
            latitude=latitude,
            longitude=longitude,
            timezone=self._timezone,
            source="client",
            precise=not coarse,
        )
        log.info(
            "client_location_resolved",
            trace_id=context.trace_id,
            city=resolution.city,
            country=resolution.country,
            source=resolution.source,
        )
        return resolution


get_location_tool = ToolDefinition(
    name="get_location",
    description=(
        "Resolve the user's current location when operator and per-user consent "
        "gates are enabled. Prefers explicit session notes and otherwise returns "
        "the user's stored client-provided device location."
    ),
    category="network",
    parameters=[
        ToolParameter(
            name="session_notes",
            type="string",
            description="Session notes or recent user text that may include an explicit location.",
            required=False,
            default=None,
            json_schema=None,
        ),
    ],
    risk_level="low",
    allowed_modes=["NORMAL", "ALERT"],
    requires_approval=False,
    requires_sandbox=False,
    timeout_seconds=10,
    rate_limit_per_hour=60,
)


async def get_location_executor(
    session_notes: str | None = None,
    *,
    ctx: TraceContext,
) -> dict[str, object]:
    """Execute location resolution.

    Args:
        session_notes: Session notes or recent user text to scan first.
        ctx: Trace context for logging and user scoping.

    Returns:
        Dict containing the resolved location, or ``resolved=False`` with a
        reason when no location can be returned.

    Raises:
        ToolExecutionError: When location features or memory service are unavailable.
    """
    trace_id = ctx.trace_id
    log.info("get_location_called", trace_id=trace_id)

    if not settings.location_enabled:
        log.info("get_location_disabled", trace_id=trace_id)
        raise ToolExecutionError("Location features are disabled.")

    user_id = ctx.user_id
    if user_id is None:
        log.info("get_location_no_user", trace_id=trace_id)
        return {"resolved": False, "reason": "no_user"}

    start_time = time.perf_counter()
    svc = _get_memory_service()
    if svc is None or not getattr(svc, "connected", False):
        log.warning("get_location_memory_unavailable", trace_id=trace_id)
        raise ToolExecutionError("Memory service unavailable or not connected.")

    consent = await svc.get_person_location_consent(str(user_id), trace_id)
    if not consent:
        log.info("get_location_consent_not_given", trace_id=trace_id)
        return {"resolved": False, "reason": "consent_not_given"}

    explicit = await ExplicitLocationProvider(session_notes).resolve(ctx)
    if explicit is not None:
        return _executor_output(explicit, start_time)

    stored = await svc.get_person_location(str(user_id), trace_id)
    if stored is None:
        log.info("get_location_no_location_stored", trace_id=trace_id)
        return {"resolved": False, "reason": "no_location_stored"}

    resolution = LocationResolution(
        city=None,
        country=None,
        latitude=_float_or_none(stored.get("latitude")),
        longitude=_float_or_none(stored.get("longitude")),
        timezone=_string_or_none(stored.get("timezone")),
        source="client",
        precise=stored.get("source") == "client" and settings.location_precision != "coarse",
    )
    log.info(
        "get_location_completed",
        trace_id=trace_id,
        resolved=True,
        city=resolution.city,
        country=resolution.country,
        source=resolution.source,
    )
    return _executor_output(resolution, start_time)


class _MemoryLocationService(Protocol):
    """Memory service methods required by get_location."""

    connected: bool

    async def get_person_location_consent(self, user_id: str, trace_id: str) -> bool:
        """Return whether the user has opted into location features."""

    async def get_person_location(self, user_id: str, trace_id: str) -> dict[str, object] | None:
        """Return the user's stored location."""


def _get_memory_service() -> _MemoryLocationService | None:
    """Resolve the global MemoryService at call time."""
    try:
        from personal_agent.service.app import memory_service as global_memory_service
    except (ImportError, AttributeError):
        return None
    return cast(_MemoryLocationService | None, global_memory_service)


def _clean_city(raw_city: str) -> str | None:
    """Normalize a parsed city candidate.

    Args:
        raw_city: Raw city text from the explicit-location regex.

    Returns:
        Cleaned city, or None when empty.
    """
    city = raw_city.strip(" .,!?:;")
    for delimiter in (" today", " tomorrow", " yesterday", " right now", " this week"):
        index = city.lower().find(delimiter)
        if index != -1:
            city = city[:index]
    return city.strip() or None


def _executor_output(resolution: LocationResolution, start_time: float) -> dict[str, object]:
    """Serialize a resolution for tool output.

    Args:
        resolution: Location resolution to serialize.
        start_time: Perf-counter start time.

    Returns:
        Tool output dictionary.
    """
    return {
        "resolved": True,
        "location": asdict(resolution),
        "latency_ms": _latency_ms(start_time),
    }


def _latency_ms(start_time: float) -> float:
    """Compute elapsed milliseconds.

    Args:
        start_time: Perf-counter start time.

    Returns:
        Elapsed milliseconds.
    """
    return (time.perf_counter() - start_time) * 1000


def _float_or_none(value: object) -> float | None:
    """Convert a stored value to float when possible."""
    if isinstance(value, int | float):
        return float(value)
    return None


def _string_or_none(value: object) -> str | None:
    """Convert a stored value to a non-empty string when possible."""
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None
