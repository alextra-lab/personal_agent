"""Native weather tool on Open-Meteo (FRE-1769).

A forecast is structured data, so it gets its own tool rather than a web_search category.
Open-Meteo is free and needs no key. A place name is geocoded with Open-Meteo's geocoding
API; coordinates skip that step. The result text has one line per hour and one per day, each
with its local time and values with units, and the result's ``url`` is the forecast request,
so grounding can cite it (``ENTRY_SOURCE_TOOLS``).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Literal

import httpx

from personal_agent.config import settings
from personal_agent.security import EgressBlockedError, create_guarded_http_client
from personal_agent.telemetry import TraceContext, get_logger
from personal_agent.tools.contract import ToolError, error_codes
from personal_agent.tools.types import ToolDefinition, ToolParameter

log = get_logger(__name__)

WeatherCode = Literal[
    "missing_location",
    "invalid_coordinates",
    "unknown_location",
    "http_error",
    "connect_failed",
    "upstream_timeout",
    "blocked_by_domain_guard",
    "weather_failed",
]


class WeatherError(ToolError[WeatherCode]):
    """A ``weather`` failure (ADR-0156 D6.1)."""


GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

_DEFAULT_DAYS = 3
_MAX_DAYS = 7
_CURRENT_FIELDS = ("temperature_2m", "precipitation", "wind_speed_10m", "weather_code")
_HOURLY_FIELDS = (
    "temperature_2m",
    "precipitation",
    "precipitation_probability",
    "wind_speed_10m",
    "weather_code",
)
_DAILY_FIELDS = ("temperature_2m_min", "temperature_2m_max", "precipitation_sum", "weather_code")

# WMO weather interpretation codes, as Open-Meteo documents them.
_WMO_CODES: Mapping[int, str] = {
    0: "Clear sky",
    1: "Mainly clear",
    2: "Partly cloudy",
    3: "Overcast",
    45: "Fog",
    48: "Depositing rime fog",
    51: "Light drizzle",
    53: "Moderate drizzle",
    55: "Dense drizzle",
    56: "Light freezing drizzle",
    57: "Dense freezing drizzle",
    61: "Slight rain",
    63: "Moderate rain",
    65: "Heavy rain",
    66: "Light freezing rain",
    67: "Heavy freezing rain",
    71: "Slight snowfall",
    73: "Moderate snowfall",
    75: "Heavy snowfall",
    77: "Snow grains",
    80: "Slight rain showers",
    81: "Moderate rain showers",
    82: "Violent rain showers",
    85: "Slight snow showers",
    86: "Heavy snow showers",
    95: "Thunderstorm",
    96: "Thunderstorm with slight hail",
    99: "Thunderstorm with heavy hail",
}

_RETRY_LATER = (
    "Call weather once more; if it fails again, tell the user the forecast is unavailable."
)


def describe_weather_code(code: object) -> str:
    """Return the words for a WMO weather code.

    Args:
        code: The ``weather_code`` value from Open-Meteo (an int, or ``None`` when missing).

    Returns:
        The description, ``"weather code N"`` for a code not in the table, or
        ``"unknown conditions"`` when there is no code.
    """
    if isinstance(code, bool) or not isinstance(code, int | float):
        return "unknown conditions"
    return _WMO_CODES.get(int(code), f"weather code {int(code)}")


weather_tool = ToolDefinition(
    name="weather",
    description=(
        "Get the weather forecast for a place from Open-Meteo: current conditions, one line "
        "per hour and one line per day, in the place's local time, with units. "
        "Use it for any weather, temperature, rain or wind question; do not use web_search "
        "for a forecast. Pass a place name as location, or latitude and longitude (they win "
        "when both are given). days is 1 to 7 (default 3). The result's url is citable. "
        'Examples: weather(location="Lyon"), weather(location="Tokyo", days=1), '
        "weather(latitude=48.85, longitude=2.35, days=7)."
    ),
    category="network",
    parameters=[
        ToolParameter(
            name="location",
            type="string",
            description="Place name to geocode, e.g. 'Lyon' or 'Berlin'. A bare city name works best.",
            required=False,
            default=None,
            json_schema=None,
        ),
        ToolParameter(
            name="latitude",
            type="number",
            description="Latitude in degrees (-90 to 90). Use with longitude instead of location.",
            required=False,
            default=None,
            json_schema=None,
        ),
        ToolParameter(
            name="longitude",
            type="number",
            description="Longitude in degrees (-180 to 180). Use with latitude instead of location.",
            required=False,
            default=None,
            json_schema=None,
        ),
        ToolParameter(
            name="days",
            type="number",
            description="Forecast days, 1 to 7 (default 3).",
            required=False,
            default=None,
            json_schema=None,
        ),
    ],
    # Same footing as web_search: a fixed, operator-chosen host (Open-Meteo), not a
    # model-chosen one as with fetch_url. Only the place name or coordinates leave.
    risk_level="low",
    allowed_modes=["NORMAL", "ALERT", "DEGRADED"],
    requires_approval=False,
    requires_sandbox=False,
    timeout_seconds=30,
    error_codes=error_codes(WeatherError),
    max_result_tokens=12_000,
    truncation_next_action="Call weather again with fewer days.",
)


def _with_unit(value: object, unit: str) -> str:
    """Format one value with its unit, ``n/a`` when the value is missing.

    Args:
        value: The number from the response, or ``None``.
        unit: The unit from the response's ``*_units`` block.

    Returns:
        ``"19.6°C"`` or ``"14.0 km/h"`` style text.
    """
    if value is None:
        return "n/a"
    if unit.startswith("°") or unit == "%":
        return f"{value}{unit}"
    return f"{value} {unit}".rstrip()


def _local(stamp: object) -> str:
    """Return an Open-Meteo ISO local time (``2026-11-11T15:00``) as ``2026-11-11 15:00``."""
    return str(stamp).replace("T", " ")


def _mapping(node: object) -> Mapping[str, object]:
    """Return ``node`` when it is a JSON object, else an empty mapping."""
    return node if isinstance(node, Mapping) else {}


def _column(block: Mapping[str, object], name: str) -> Sequence[object]:
    """Return one column of an hourly or daily block, empty when absent."""
    values = block.get(name)
    return values if isinstance(values, list) else []


def _units(data: Mapping[str, object], key: str) -> dict[str, str]:
    """Return a ``*_units`` block as name -> unit text."""
    return {k: str(v) for k, v in _mapping(data.get(key)).items()}


def format_forecast(data: Mapping[str, object], place: str) -> str:
    """Render an Open-Meteo forecast response as text, one line per day and per hour.

    Args:
        data: The parsed forecast response.
        place: How to name the place in the header line.

    Returns:
        The text. Times are the place's local time (the request uses ``timezone=auto``).
    """
    tz = data.get("timezone", "local time")
    abbr = data.get("timezone_abbreviation")
    zone = f"{tz} ({abbr})" if abbr and abbr != tz else str(tz)
    lines = [f"Weather for {place}. Times are local, {zone}. Source: Open-Meteo."]

    current = _mapping(data.get("current"))
    if current:
        cu = _units(data, "current_units")
        lines.append(
            f"Current ({_local(current.get('time'))}): "
            f"{_with_unit(current.get('temperature_2m'), cu.get('temperature_2m', ''))}, "
            f"{describe_weather_code(current.get('weather_code'))}, "
            f"precipitation {_with_unit(current.get('precipitation'), cu.get('precipitation', ''))}, "
            f"wind {_with_unit(current.get('wind_speed_10m'), cu.get('wind_speed_10m', ''))}"
        )

    daily = _mapping(data.get("daily"))
    du = _units(data, "daily_units")
    cols = {name: _column(daily, name) for name in _DAILY_FIELDS}
    lines.append("Daily:")
    for i, day in enumerate(_column(daily, "time")):
        row = {name: (col[i] if i < len(col) else None) for name, col in cols.items()}
        lines.append(
            f"{day}: {describe_weather_code(row['weather_code'])}, "
            f"min {_with_unit(row['temperature_2m_min'], du.get('temperature_2m_min', ''))}, "
            f"max {_with_unit(row['temperature_2m_max'], du.get('temperature_2m_max', ''))}, "
            f"precipitation {_with_unit(row['precipitation_sum'], du.get('precipitation_sum', ''))}"
        )

    hourly = _mapping(data.get("hourly"))
    hu = _units(data, "hourly_units")
    cols = {name: _column(hourly, name) for name in _HOURLY_FIELDS}
    lines.append("Hourly:")
    for i, stamp in enumerate(_column(hourly, "time")):
        row = {name: (col[i] if i < len(col) else None) for name, col in cols.items()}
        probability = _with_unit(
            row["precipitation_probability"], hu.get("precipitation_probability", "")
        )
        lines.append(
            f"{_local(stamp)}: "
            f"{_with_unit(row['temperature_2m'], hu.get('temperature_2m', ''))}, "
            f"{describe_weather_code(row['weather_code'])}, "
            f"precipitation {_with_unit(row['precipitation'], hu.get('precipitation', ''))} "
            f"({probability} chance), "
            f"wind {_with_unit(row['wind_speed_10m'], hu.get('wind_speed_10m', ''))}"
        )
    return "\n".join(lines)


async def _get_json(client: httpx.AsyncClient, url: str, *, trace_id: str) -> Mapping[str, object]:
    """GET one Open-Meteo URL and return its JSON object.

    Args:
        client: The guarded client.
        url: The full request URL.
        trace_id: Trace id for logging.

    Returns:
        The parsed JSON object.

    Raises:
        WeatherError: ``http_error`` on a non-2xx status, ``weather_failed`` on a body that
            is not a JSON object.
    """
    resp = await client.get(url)
    if resp.is_error:
        reason = ""
        try:
            reason = str(_mapping(resp.json()).get("reason") or "")
        except ValueError:
            pass
        log.error("weather_http_error", trace_id=trace_id, status=resp.status_code, url=url)
        temporary = resp.status_code == 429 or resp.status_code >= 500
        raise WeatherError(
            "http_error",
            f"Open-Meteo returned HTTP {resp.status_code}" + (f": {reason}" if reason else "."),
            retryable=temporary,
            next_action=_RETRY_LATER if temporary else "Check the arguments; do not retry as is.",
        )
    try:
        data = resp.json()
    except ValueError as exc:
        raise WeatherError(
            "weather_failed",
            "Open-Meteo returned a body that is not JSON.",
            retryable=True,
            next_action=_RETRY_LATER,
        ) from exc
    if not isinstance(data, Mapping):
        raise WeatherError(
            "weather_failed",
            "Open-Meteo returned JSON that is not an object.",
            retryable=True,
            next_action=_RETRY_LATER,
        )
    return data


def _coordinate(value: object, name: str, bound: float) -> float:
    """Parse and range-check one coordinate.

    Args:
        value: The model's argument.
        name: ``latitude`` or ``longitude``, for the message.
        bound: 90 or 180.

    Returns:
        The coordinate as a float.

    Raises:
        WeatherError: ``invalid_coordinates`` when it is not a number in range.
    """
    try:
        number = float(str(value))
    except ValueError:
        number = float("nan")
    if not -bound <= number <= bound:
        raise WeatherError(
            "invalid_coordinates",
            f"{name} must be a number from {-bound:g} to {bound:g}, got {value!r}.",
            retryable=True,
            next_action="Call weather again with valid coordinates, or with a location name.",
        )
    return number


async def weather_executor(
    location: str | None = None,
    latitude: float | None = None,
    longitude: float | None = None,
    days: int | None = None,
    *,
    ctx: TraceContext,
) -> dict[str, object]:
    """Fetch a forecast from Open-Meteo for a place name or coordinates.

    Args:
        location: Place name to geocode. Ignored when both coordinates are given.
        latitude: Latitude in degrees.
        longitude: Longitude in degrees.
        days: Forecast days, clamped to 1-7 (default 3).
        ctx: Trace context for logging.

    Returns:
        Dict with ``url`` (the forecast request, citable), ``location``, ``timezone``,
        ``days`` and ``text`` (one line per day and per hour, local time, with units).

    Raises:
        WeatherError: No location and no coordinates, invalid coordinates, an unknown place,
            or an Open-Meteo failure (timeout, HTTP error, connection, blocked, bad body).
    """
    trace_id = ctx.trace_id
    location = (location or "").strip()
    forecast_days = max(1, min(_DEFAULT_DAYS if days is None else int(days), _MAX_DAYS))

    if latitude is None and longitude is None and not location:
        raise WeatherError(
            "missing_location",
            "Give a location name, or both latitude and longitude.",
            retryable=True,
            next_action='Call weather again with location, e.g. weather(location="Lyon").',
        )
    if (latitude is None) != (longitude is None) and not location:
        raise WeatherError(
            "invalid_coordinates",
            "latitude and longitude must be given together.",
            retryable=True,
            next_action="Call weather again with both coordinates, or with a location name.",
        )

    log.info(
        "weather_started",
        trace_id=trace_id,
        location=location or None,
        latitude=latitude,
        longitude=longitude,
        days=forecast_days,
    )

    url = ""
    try:
        async with create_guarded_http_client(timeout=settings.weather_timeout_seconds) as client:
            place: dict[str, object]
            if latitude is not None and longitude is not None:
                lat = _coordinate(latitude, "latitude", 90)
                lon = _coordinate(longitude, "longitude", 180)
                place = {"latitude": lat, "longitude": lon}
                label = f"{lat:g}, {lon:g}"
            else:
                url = str(httpx.URL(GEOCODING_URL, params={"name": location, "count": 1}))
                geo = await _get_json(client, url, trace_id=trace_id)
                results = geo.get("results")
                if not isinstance(results, list) or not results:
                    log.info("weather_unknown_location", trace_id=trace_id, location=location)
                    raise WeatherError(
                        "unknown_location",
                        f"Open-Meteo found no place named '{location}'.",
                        retryable=False,
                        next_action=(
                            "Call weather again with a bare city name (no region or country "
                            "words), or with latitude and longitude."
                        ),
                    )
                hit = _mapping(results[0])
                lat = _coordinate(hit.get("latitude"), "latitude", 90)
                lon = _coordinate(hit.get("longitude"), "longitude", 180)
                place = {
                    "name": hit.get("name"),
                    "admin1": hit.get("admin1"),
                    "country": hit.get("country"),
                    "latitude": lat,
                    "longitude": lon,
                }
                parts = (hit.get("name"), hit.get("admin1"), hit.get("country"))
                label = f"{', '.join(str(p) for p in parts if p)} ({lat:g}, {lon:g})"

            url = str(
                httpx.URL(
                    FORECAST_URL,
                    params={
                        "latitude": lat,
                        "longitude": lon,
                        "current": ",".join(_CURRENT_FIELDS),
                        "hourly": ",".join(_HOURLY_FIELDS),
                        "daily": ",".join(_DAILY_FIELDS),
                        "timezone": "auto",
                        "forecast_days": forecast_days,
                    },
                )
            )
            data = await _get_json(client, url, trace_id=trace_id)
    except WeatherError:
        raise
    except EgressBlockedError as exc:
        log.warning("weather_blocked", trace_id=trace_id, url=url, reason=exc.reason)
        raise WeatherError(
            "blocked_by_domain_guard",
            f"Open-Meteo is blocked by the domain guard [{exc.reason}].",
            retryable=False,
            next_action="Do not retry. Tell the user the forecast service is blocked.",
        ) from exc
    except httpx.ConnectError as exc:
        log.error("weather_connect_failed", trace_id=trace_id, url=url)
        raise WeatherError(
            "connect_failed",
            "Cannot connect to Open-Meteo.",
            retryable=True,
            next_action=_RETRY_LATER,
        ) from exc
    except httpx.TimeoutException as exc:
        log.error("weather_timeout", trace_id=trace_id, url=url)
        raise WeatherError(
            "upstream_timeout",
            f"Open-Meteo did not answer within {settings.weather_timeout_seconds}s.",
            retryable=True,
            next_action=_RETRY_LATER,
        ) from exc
    except Exception as exc:
        log.error("weather_failed", trace_id=trace_id, url=url, error=str(exc), exc_info=True)
        raise WeatherError(
            "weather_failed",
            f"The forecast request failed: {type(exc).__name__}: {exc}",
            retryable=False,
            next_action="Tell the user the forecast is unavailable.",
        ) from exc

    text = format_forecast(data, label)
    log.info(
        "weather_completed",
        trace_id=trace_id,
        url=url,
        timezone=data.get("timezone"),
        days=forecast_days,
        char_count=len(text),
    )
    return {
        "url": url,
        "location": place,
        "timezone": data.get("timezone"),
        "days": forecast_days,
        "text": text,
    }
