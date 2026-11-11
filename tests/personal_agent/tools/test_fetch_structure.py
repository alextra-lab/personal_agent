"""FRE-1764: ``fetch_url`` keeps the structure of JSON parallel arrays and HTML tables.

Mocked responses only, no network. The fixtures are saved, small, realistic pages: an
Open-Meteo-shaped forecast and a page holding one timetable table.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from personal_agent.telemetry.trace import TraceContext
from personal_agent.tools.fetch import fetch_url_executor

_CTX = TraceContext.new_trace()
_FIXTURES_DIR = Path(__file__).parent / "fixtures" / "fetch"


async def _fetch(body: str, content_type: str) -> dict[str, object]:
    """Run ``fetch_url`` on one mocked response body."""
    resp = httpx.Response(200, content=body.encode(), headers={"content-type": content_type})
    client = AsyncMock()
    client.get = AsyncMock(return_value=resp)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    with patch("personal_agent.tools.fetch.create_guarded_http_client", return_value=client):
        return await fetch_url_executor(url="https://api.example.com/v1/forecast", ctx=_CTX)


async def _json_text(document: object) -> str:
    result = await _fetch(json.dumps(document), "application/json; charset=utf-8")
    return str(result["text"])


# ── AC-1: parallel arrays reach the model one index per line ──────────────


@pytest.mark.asyncio
async def test_weather_api_parallel_arrays_put_each_hour_on_one_line() -> None:
    """AC-1: the 15:00 values of the saved forecast sit on one line, with their units."""
    body = (_FIXTURES_DIR / "open_meteo_forecast.json").read_text(encoding="utf-8")
    result = await _fetch(body, "application/json; charset=utf-8")
    lines = str(result["text"]).splitlines()

    assert (
        "2026-11-11T15:00 | temperature_2m=12.3 °C | precipitation=0.0 mm"
        " | wind_speed_10m=16.1 km/h"
    ) in lines
    assert "2026-11-11T12:00 | temperature_2m=11.1 °C | precipitation=0.2 mm" in "\n".join(lines)
    # The daily block is grouped the same way, under its own heading.
    assert "2026-11-12 | temperature_2m_max=10.9 °C | temperature_2m_min=5.2 °C" in lines
    # The positional arrays are gone; the other fields stay in full.
    assert "[11.1, 11.9" not in str(result["text"])
    assert "[11.1,11.9" not in str(result["text"])
    for kept in ('"timezone": "Europe/Paris"', '"elevation": 43.0', '"latitude": 48.86'):
        assert kept in str(result["text"])


@pytest.mark.asyncio
async def test_rows_follow_the_index_order_and_count() -> None:
    """The index key leads each row; rows keep array order; the heading counts them."""
    text = await _json_text(
        {"hourly": {"temp": [1, 2, 3], "time": ["a", "b", "c"], "flag": [True, None, False]}}
    )
    lines = text.splitlines()
    rows = [line for line in lines if line.startswith(("a |", "b |", "c |"))]
    assert rows == ["a | temp=1 | flag=true", "b | temp=2 | flag=null", "c | temp=3 | flag=false"]
    assert any("hourly" in line and "3 rows" in line for line in lines)


@pytest.mark.asyncio
async def test_nested_object_with_parallel_arrays_is_found() -> None:
    """Parallel arrays inside a list of objects are found and rendered."""
    text = await _json_text(
        {"results": [{"name": "Paris", "series": {"date": ["d1", "d2"], "rain": [0.5, 1.5]}}]}
    )
    assert "d1 | rain=0.5" in text.splitlines()
    assert "d2 | rain=1.5" in text.splitlines()
    assert '"name": "Paris"' in text


@pytest.mark.asyncio
async def test_ragged_arrays_are_not_paired() -> None:
    """Arrays of different lengths are not row-aligned; the array of another length stays."""
    document = {"time": ["a", "b", "c"], "temp": [1, 2, 3], "note": [7, 8]}
    text = await _json_text(document)
    assert "a | temp=1" in text.splitlines()
    assert '"note": [7, 8]' in text
    assert "note=" not in text


@pytest.mark.asyncio
async def test_arrays_of_equal_length_without_an_index_key_stay_as_they_are() -> None:
    """No time or index key: the arrays are not paired and the body is unchanged."""
    document = {"xs": [1, 2, 3], "ys": [4, 5, 6]}
    body = json.dumps(document)
    result = await _fetch(body, "application/json")
    assert result["text"] == body


# ── AC-2: other JSON keeps its full content ───────────────────────────────


@pytest.mark.asyncio
async def test_json_without_parallel_arrays_is_returned_in_full() -> None:
    """AC-2: an object with no parallel arrays comes back byte for byte."""
    body = json.dumps(
        {
            "name": "Seshat",
            "tags": ["a", "b", "c"],
            "time": ["only one array"],
            "nested": {"count": 3, "items": [{"id": 1}, {"id": 2}]},
            "unicode": "café",
        },
        ensure_ascii=False,
    )
    result = await _fetch(body, "application/json")
    assert result["text"] == body


@pytest.mark.asyncio
async def test_invalid_json_is_returned_as_it_came() -> None:
    """A body that does not parse as JSON is returned unchanged."""
    body = '{"time": ["a", "b"], "temp": [1, 2'
    result = await _fetch(body, "application/json")
    assert result["text"] == body


@pytest.mark.asyncio
async def test_json_nested_too_deep_is_returned_as_it_came() -> None:
    """A body too deep to walk is returned unchanged, not raised as a failure."""
    body = "[" * 100_000 + "]" * 100_000
    result = await _fetch(body, "application/json")
    assert result["text"] == body[:10_000]


@pytest.mark.asyncio
async def test_arrays_of_objects_are_not_rows() -> None:
    """An array of objects is already one record per element: it is left alone."""
    body = json.dumps({"time": [{"h": 1}, {"h": 2}], "temp": [{"v": 1}, {"v": 2}]})
    result = await _fetch(body, "application/json")
    assert result["text"] == body


# ── AC-3: an HTML table reaches the model one row per line ────────────────


@pytest.mark.asyncio
async def test_html_table_has_one_line_per_row_with_a_cell_separator() -> None:
    """AC-3: header row first, then one line per row, cells joined by ' | '."""
    body = (_FIXTURES_DIR / "table_page.html").read_text(encoding="utf-8")
    result = await _fetch(body, "text/html; charset=utf-8")
    lines = str(result["text"]).splitlines()

    header = "Day | Departure time | Arrival time | Fare (EUR)"
    assert header in lines
    assert "Monday | 08:30 | 18:15 | 129" in lines
    assert "Wednesday | 10:45 | 20:30 | 139" in lines
    # colspan=2 keeps the next cell under its own column.
    assert "Saturday | No sailing | | -" in lines
    assert lines.index(header) < lines.index("Monday | 08:30 | 18:15 | 129")
    # The text after the table no longer joins the last row.
    assert "Fares are per adult, one way." in lines
    # Navigation and footer stay excluded.
    assert "Home" not in str(result["text"])


@pytest.mark.asyncio
async def test_nested_table_rows_do_not_leak_separators_into_the_outer_row() -> None:
    """A nested table keeps its own rows; the outer row keeps its separators."""
    body = (
        "<table><tr><td>Outer A</td><td>"
        "<table><tr><td>in 1</td><td>in 2</td></tr></table>"
        "</td><td>Outer C</td></tr><tr><td>Next</td><td>row</td></tr></table>"
    )
    result = await _fetch(body, "text/html")
    lines = str(result["text"]).splitlines()
    assert "in 1 | in 2" in lines
    assert "Next | row" in lines
    assert any(line.startswith("Outer A |") for line in lines)
    assert any(line.endswith("| Outer C") for line in lines)


@pytest.mark.asyncio
async def test_table_inside_a_skipped_region_adds_no_separators() -> None:
    """A table inside nav adds no stray separator to the text."""
    body = "<nav><table><tr><td>x</td><td>y</td></tr></table></nav><p>Body text</p>"
    result = await _fetch(body, "text/html")
    assert result["text"] == "Body text"


@pytest.mark.asyncio
async def test_huge_colspan_is_bounded() -> None:
    """A hostile colspan cannot blow up the extracted text."""
    body = '<table><tr><td colspan="99999999">a</td><td>b</td></tr></table>'
    result = await _fetch(body, "text/html")
    assert str(result["text"]).count("|") <= 100


# ── AC-4: no other output field changes shape ─────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "content_type"),
    [
        (
            (_FIXTURES_DIR / "open_meteo_forecast.json").read_text(encoding="utf-8"),
            "application/json",
        ),
        ((_FIXTURES_DIR / "table_page.html").read_text(encoding="utf-8"), "text/html"),
    ],
)
async def test_output_fields_keep_their_shape(body: str, content_type: str) -> None:
    """AC-4: the result has the same keys and types; only ``text`` content changes."""
    result = await _fetch(body, content_type)
    assert set(result) == {"url", "text", "char_count", "truncated"}
    assert isinstance(result["text"], str)
    assert result["char_count"] == len(result["text"])
    assert result["truncated"] is False


@pytest.mark.asyncio
async def test_rendered_json_still_obeys_max_chars() -> None:
    """The rendered rows still go through the max_chars cap."""
    hours = [f"2026-11-11T{h:02d}:00" for h in range(24)] * 20
    document = {"hourly": {"time": hours, "temperature_2m": [1.5] * len(hours)}}
    resp = httpx.Response(
        200, content=json.dumps(document).encode(), headers={"content-type": "application/json"}
    )
    client = AsyncMock()
    client.get = AsyncMock(return_value=resp)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    with patch("personal_agent.tools.fetch.create_guarded_http_client", return_value=client):
        result = await fetch_url_executor(url="https://api.example.com", max_chars=500, ctx=_CTX)
    assert result["char_count"] == 500
    assert result["truncated"] is True
