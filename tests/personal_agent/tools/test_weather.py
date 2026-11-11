"""Tests for the native ``weather`` tool (FRE-1769).

Open-Meteo responses come from saved fixtures recorded on 2026-11-11 (Lyon, two forecast
days). HTTP goes through an ``httpx.MockTransport``, so no test reaches the network.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from personal_agent.grounding.source_registry import (
    ENTRY_SOURCE_TOOLS,
    TYPED_RETRIEVAL_TOOLS,
    url_entries,
)
from personal_agent.security import EgressBlockedError
from personal_agent.telemetry.trace import TraceContext
from personal_agent.tools import weather as weather_module
from personal_agent.tools.weather import WeatherError, weather_executor, weather_tool

_FIXTURES = Path(__file__).parent / "fixtures" / "weather"
_FORECAST = json.loads((_FIXTURES / "forecast_lyon_2026_11_11.json").read_text())
_GEOCODE = json.loads((_FIXTURES / "geocode_lyon.json").read_text())
_CTX = TraceContext.new_trace()

Handler = Callable[[httpx.Request], httpx.Response]


def _install(monkeypatch: pytest.MonkeyPatch, handler: Handler) -> list[httpx.Request]:
    """Route the tool's HTTP client through ``handler`` and record each request."""
    seen: list[httpx.Request] = []

    def recording(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    def factory(**kwargs: object) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(recording), **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(weather_module, "create_guarded_http_client", factory)
    return seen


def _open_meteo(request: httpx.Request) -> httpx.Response:
    if request.url.host == "geocoding-api.open-meteo.com":
        if request.url.params.get("name") == "Lyon":
            return httpx.Response(200, json=_GEOCODE)
        return httpx.Response(200, json={"generationtime_ms": 0.08})
    return httpx.Response(200, json=_FORECAST)


# ── AC-1: inputs ─────────────────────────────────────────────────────────────


def test_tool_parameters() -> None:
    """AC-1: location, latitude, longitude and days, all optional."""
    params = {p.name: p for p in weather_tool.parameters}
    assert set(params) == {"location", "latitude", "longitude", "days"}
    assert not any(p.required for p in params.values())


@pytest.mark.asyncio
async def test_location_is_geocoded(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-1: a place name goes to the geocoding API, its coordinates to the forecast."""
    seen = _install(monkeypatch, _open_meteo)
    out = await weather_executor(location="Lyon", ctx=_CTX)

    geo = seen[0].url
    assert (
        f"{geo.scheme}://{geo.host}{geo.path}" == "https://geocoding-api.open-meteo.com/v1/search"
    )
    assert geo.params["name"] == "Lyon"
    assert geo.params["count"] == "1"
    forecast = seen[1].url
    assert forecast.params["latitude"] == "45.74906"
    assert forecast.params["longitude"] == "4.84789"
    assert forecast.params["forecast_days"] == "3"  # default
    assert out["location"]["name"] == "Lyon"
    assert out["location"]["country"] == "France"


@pytest.mark.asyncio
async def test_coordinates_skip_geocoding(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-1: coordinates go straight to the forecast."""
    seen = _install(monkeypatch, _open_meteo)
    await weather_executor(latitude=45.75, longitude=4.85, days=2, ctx=_CTX)

    assert len(seen) == 1
    assert seen[0].url.host == "api.open-meteo.com"
    assert seen[0].url.params["latitude"] == "45.75"
    assert seen[0].url.params["forecast_days"] == "2"


@pytest.mark.asyncio
@pytest.mark.parametrize(("days", "sent"), [(0, "1"), (9, "7"), (5, "5")])
async def test_days_is_clamped_to_1_7(
    monkeypatch: pytest.MonkeyPatch, days: int, sent: str
) -> None:
    """AC-1: days outside 1-7 is clamped, not refused."""
    seen = _install(monkeypatch, _open_meteo)
    await weather_executor(latitude=45.75, longitude=4.85, days=days, ctx=_CTX)
    assert seen[0].url.params["forecast_days"] == sent


# ── AC-2: forecast request and text ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_forecast_request_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-2: the forecast asks for current, hourly and daily fields with timezone=auto."""
    seen = _install(monkeypatch, _open_meteo)
    await weather_executor(latitude=45.75, longitude=4.85, ctx=_CTX)

    url = seen[0].url
    assert f"{url.scheme}://{url.host}{url.path}" == "https://api.open-meteo.com/v1/forecast"
    assert url.params["timezone"] == "auto"
    assert set(url.params["hourly"].split(",")) == {
        "temperature_2m",
        "precipitation",
        "precipitation_probability",
        "wind_speed_10m",
        "weather_code",
    }
    assert set(url.params["daily"].split(",")) == {
        "temperature_2m_max",
        "temperature_2m_min",
        "precipitation_sum",
        "weather_code",
    }
    assert url.params["current"]


@pytest.mark.asyncio
async def test_the_15_00_values_sit_on_one_line(monkeypatch: pytest.MonkeyPatch) -> None:
    """The saved response holds 19.6 °C, 0.0 mm, 10 %, 14.0 km/h, code 3 at 15:00 on 11 Nov."""
    _install(monkeypatch, _open_meteo)
    out = await weather_executor(location="Lyon", ctx=_CTX)

    lines = [ln for ln in out["text"].splitlines() if ln.startswith("2026-11-11 15:00")]
    assert len(lines) == 1
    line = lines[0]
    for value in ("19.6°C", "Overcast", "0.0 mm", "10%", "14.0 km/h"):
        assert value in line, (value, line)


@pytest.mark.asyncio
async def test_one_line_per_hour_and_per_day(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-2: every hour and every day of the saved response has exactly one line."""
    _install(monkeypatch, _open_meteo)
    out = await weather_executor(location="Lyon", ctx=_CTX)
    text = out["text"]

    for stamp in _FORECAST["hourly"]["time"]:
        local = stamp.replace("T", " ")
        assert sum(ln.startswith(local) for ln in text.splitlines()) == 1, local
    day_lines = [ln for ln in text.splitlines() if ln.startswith("2026-11-12:")]
    assert len(day_lines) == 1
    assert "°C" in day_lines[0] and "mm" in day_lines[0]
    assert "Europe/Paris" in text
    assert "Current (2026-11-11 17:00)" in text


def test_weather_code_words() -> None:
    """AC-2: WMO codes become words."""
    assert weather_module.describe_weather_code(0) == "Clear sky"
    assert weather_module.describe_weather_code(61) == "Slight rain"
    assert weather_module.describe_weather_code(95) == "Thunderstorm"
    assert weather_module.describe_weather_code(None) == "unknown conditions"
    assert weather_module.describe_weather_code(42) == "weather code 42"


# ── AC-3: citable url ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_result_url_is_the_forecast_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-3: the result's url is the forecast request, and url_entries finds it."""
    seen = _install(monkeypatch, _open_meteo)
    out = await weather_executor(location="Lyon", ctx=_CTX)

    assert out["url"] == str(seen[1].url)
    query = parse_qs(urlsplit(out["url"]).query)
    assert query["timezone"] == ["auto"]
    # grounding finds the url as an entry of the result (FRE-1662 path)
    entries = url_entries(json.loads(json.dumps(out)))
    assert [e["url"] for e in entries] == [out["url"]]


@pytest.mark.asyncio
async def test_the_result_registers_with_its_url_as_referent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC-3: grounding registers the result as a source whose referent is the forecast URL."""
    from personal_agent.grounding.source_registry import Admissibility, SourceRegistry

    _install(monkeypatch, _open_meteo)
    arguments = {"location": "Lyon"}
    out = await weather_executor(location="Lyon", ctx=_CTX)
    (entry,) = url_entries(json.loads(json.dumps(out)))

    registration = SourceRegistry(turn_id="t-weather").register_result_entry(
        tool_name="weather", arguments=arguments, entry=entry
    )
    assert registration.admissibility is Admissibility.ADMISSIBLE
    assert registration.source is not None
    assert registration.source.referent == out["url"]


def test_weather_is_a_citable_entry_source() -> None:
    """AC-3: grounding admits the result and registers its url entry."""
    assert "weather" in TYPED_RETRIEVAL_TOOLS
    assert "weather" in ENTRY_SOURCE_TOOLS


# ── AC-4: errors are results, not crashes ────────────────────────────────────


@pytest.mark.asyncio
async def test_unknown_place(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-4: an unknown place is a typed unknown_location failure."""
    _install(monkeypatch, _open_meteo)
    with pytest.raises(WeatherError) as info:
        await weather_executor(location="Xyzzyplughville", ctx=_CTX)
    assert info.value.code == "unknown_location"
    assert "Xyzzyplughville" in info.value.message
    assert info.value.retryable is False


@pytest.mark.asyncio
async def test_no_location_and_no_coordinates() -> None:
    """AC-4: a call with no place is a typed failure."""
    with pytest.raises(WeatherError) as info:
        await weather_executor(ctx=_CTX)
    assert info.value.code == "missing_location"


@pytest.mark.asyncio
@pytest.mark.parametrize(("lat", "lon"), [(45.0, None), (None, 4.0), (91.0, 4.0), (45.0, -181.0)])
async def test_bad_coordinates(lat: float | None, lon: float | None) -> None:
    """AC-4: a lone or out-of-range coordinate is a typed failure."""
    with pytest.raises(WeatherError) as info:
        await weather_executor(latitude=lat, longitude=lon, ctx=_CTX)
    assert info.value.code == "invalid_coordinates"


@pytest.mark.asyncio
async def test_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-4: a timeout is a retryable upstream_timeout failure."""

    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    _install(monkeypatch, slow)
    with pytest.raises(WeatherError) as info:
        await weather_executor(location="Lyon", ctx=_CTX)
    assert info.value.code == "upstream_timeout"
    assert info.value.retryable is True


@pytest.mark.asyncio
async def test_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-4: a 5xx is a retryable http_error failure."""
    _install(monkeypatch, lambda r: httpx.Response(503, json={"reason": "down"}))
    with pytest.raises(WeatherError) as info:
        await weather_executor(latitude=45.0, longitude=4.0, ctx=_CTX)
    assert info.value.code == "http_error"
    assert "503" in info.value.message
    assert info.value.retryable is True


@pytest.mark.asyncio
async def test_http_400_is_not_retryable(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC-4: a 4xx is a non-retryable http_error carrying Open-Meteo's reason."""
    _install(monkeypatch, lambda r: httpx.Response(400, json={"error": True, "reason": "bad"}))
    with pytest.raises(WeatherError) as info:
        await weather_executor(latitude=45.0, longitude=4.0, ctx=_CTX)
    assert info.value.code == "http_error"
    assert "bad" in info.value.message
    assert info.value.retryable is False


def _raise(exc_type: type[Exception]) -> Handler:
    """Build a handler that raises ``exc_type`` for the request it gets."""

    def handler(request: httpx.Request) -> httpx.Response:
        if exc_type is EgressBlockedError:
            raise EgressBlockedError(request, "blocklisted", "open-meteo.com")
        if issubclass(exc_type, httpx.RequestError):
            raise exc_type("boom", request=request)
        raise exc_type("boom")

    return handler


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("handler", "code", "retryable"),
    [
        (lambda r: httpx.Response(200, text="<html>not json</html>"), "weather_failed", True),
        (lambda r: httpx.Response(200, json=[1, 2]), "weather_failed", True),
        (_raise(EgressBlockedError), "blocked_by_domain_guard", False),
        (_raise(httpx.ConnectError), "connect_failed", True),
        (_raise(RuntimeError), "weather_failed", False),
    ],
    ids=["not_json", "not_object", "blocked", "connect", "unexpected"],
)
async def test_other_failures_are_typed(
    monkeypatch: pytest.MonkeyPatch, handler: Handler, code: str, retryable: bool
) -> None:
    """AC-4: a bad body, a guard block, a connect failure and a crash are typed failures."""
    _install(monkeypatch, handler)
    with pytest.raises(WeatherError) as info:
        await weather_executor(latitude=45.0, longitude=4.0, ctx=_CTX)
    assert info.value.code == code
    assert info.value.retryable is retryable


@pytest.mark.asyncio
async def test_unknown_place_is_an_error_result_through_the_layer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Through the execution layer the failure is a ToolResult, not an exception."""
    from personal_agent.brainstem.mode_manager import ModeManager
    from personal_agent.config.governance_loader import load_governance_config
    from personal_agent.governance.models import Mode
    from personal_agent.tools.executor import ToolExecutionLayer
    from personal_agent.tools.registry import ToolRegistry

    _install(monkeypatch, _open_meteo)
    reg = ToolRegistry()
    reg.register(weather_tool, weather_executor)
    gov = load_governance_config()
    mode_mgr = ModeManager(governance_config=gov)
    monkeypatch.setattr(mode_mgr, "get_current_mode", lambda: Mode.NORMAL)
    layer = ToolExecutionLayer(registry=reg, governance_config=gov, mode_manager=mode_mgr)

    result = await layer.execute_tool(
        "weather", {"location": "Xyzzyplughville"}, TraceContext.new_trace()
    )
    assert result.success is False
    assert result.error_detail is not None
    assert result.error_detail.code == "unknown_location"


# ── AC-3: registration and governance ────────────────────────────────────────


def test_registered_in_default_registry() -> None:
    """AC-3: the default registry offers weather in NORMAL mode."""
    from personal_agent.governance.models import Mode
    from personal_agent.tools import get_default_registry

    registry = get_default_registry()
    assert "weather" in {t.name for t in registry.list_tools(mode=Mode.NORMAL)}


def test_governance_entries() -> None:
    """AC-3: tools.yaml and permissions.yaml carry weather."""
    import yaml

    root = Path(__file__).resolve().parents[3] / "config" / "governance"
    tools = yaml.safe_load((root / "tools.yaml").read_text())["tools"]
    assert tools["weather"]["allowed_in_modes"] == ["NORMAL", "ALERT", "DEGRADED"]
    allow = yaml.safe_load((root / "permissions.yaml").read_text())["allow"]
    assert {"rule": "weather"} in allow
