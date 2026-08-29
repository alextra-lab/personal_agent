"""Unit tests for NovelDestinationTracker — FRE-1330.

Observe-only signal: the first time fetch_url targets a registered-domain not seen
in the prior N days, a distinct event should fire (AC-1). A repeat destination inside
the window must NOT fire (AC-2, the seeded negative — a tracker that fires on every
call is as useless as one that never fires).
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from personal_agent.security import (
    NovelDestinationTracker,
    NoveltyResult,
    _registrable_domain,
)

# ---------------------------------------------------------------------------
# _registrable_domain — eTLD+1 approximation
# ---------------------------------------------------------------------------


class TestRegistrableDomain:
    @pytest.mark.parametrize(
        ("hostname", "expected"),
        [
            ("docs.exa.ai", "exa.ai"),
            ("exa.ai", "exa.ai"),
            ("raw.githubusercontent.com", "githubusercontent.com"),
            ("github.com", "github.com"),
            ("en.wikipedia.org", "wikipedia.org"),
            ("fr.wikipedia.org", "wikipedia.org"),
            ("climate-api.open-meteo.com", "open-meteo.com"),
            ("marine-api.open-meteo.com", "open-meteo.com"),
        ],
    )
    def test_collapses_subdomains_to_etld_plus_1(self, hostname: str, expected: str) -> None:
        assert _registrable_domain(hostname) == expected

    def test_multi_part_public_suffix_is_a_known_gap(self) -> None:
        """Documented residual gap: 'co.uk' collapses like any other 2-label suffix.

        Not asserted as correct — asserted as the known, stated behaviour, so a future
        change to this function trips this test rather than silently drifting.
        """
        assert _registrable_domain("a.co.uk") == "co.uk"

    def test_single_label_host_passes_through(self) -> None:
        assert _registrable_domain("localhost") == "localhost"

    def test_ipv4_literal_is_not_collapsed_by_trailing_octets(self) -> None:
        """An IP literal has no eTLD+1 — collapsing by its last two octets would group
        unrelated hosts together (203.0.113.5 and 198.51.113.5 both -> '113.5').
        """
        assert _registrable_domain("203.0.113.5") == "203.0.113.5"

    def test_ipv6_literal_passes_through(self) -> None:
        assert _registrable_domain("::1") == "::1"

    def test_empty_string_passes_through(self) -> None:
        assert _registrable_domain("") == ""

    def test_trailing_dot_fqdn_is_normalised(self) -> None:
        assert _registrable_domain("exa.ai.") == "exa.ai"


# ---------------------------------------------------------------------------
# check_and_record — AC-1 / AC-2
# ---------------------------------------------------------------------------


def _tracker(tmp_path: Path, *, window_seconds: float = 14 * 86400.0) -> NovelDestinationTracker:
    return NovelDestinationTracker(
        cache_path=tmp_path / "egress_novelty_seen.json", window_seconds=window_seconds
    )


class TestFirstSighting:
    @pytest.mark.asyncio
    async def test_first_sighting_of_a_domain_is_novel(self, tmp_path: Path) -> None:
        tracker = _tracker(tmp_path)
        result = await tracker.check_and_record("https://never-seen-before.example/page")
        assert result == NoveltyResult(novel=True, registrable_domain="never-seen-before.example")

    @pytest.mark.asyncio
    async def test_first_sighting_is_persisted_to_disk(self, tmp_path: Path) -> None:
        cache_path = tmp_path / "egress_novelty_seen.json"
        tracker = NovelDestinationTracker(cache_path=cache_path)
        await tracker.check_and_record("https://new-domain.example/page")

        data = json.loads(cache_path.read_text())
        assert "new-domain.example" in data


class TestSeededNegative:
    @pytest.mark.asyncio
    async def test_repeat_sighting_inside_window_is_not_novel(self, tmp_path: Path) -> None:
        """AC-2: a domain fetched once already must not re-alert on a second fetch."""
        tracker = _tracker(tmp_path)
        first = await tracker.check_and_record("https://known-domain.example/a")
        second = await tracker.check_and_record("https://known-domain.example/b")

        assert first.novel is True
        assert second.novel is False

    @pytest.mark.asyncio
    async def test_cross_subdomain_sighting_collapses_to_same_key(self, tmp_path: Path) -> None:
        """docs.exa.ai then exa.ai must be treated as the same site, not two novel ones."""
        tracker = _tracker(tmp_path)
        first = await tracker.check_and_record("https://docs.exa.ai/guide")
        second = await tracker.check_and_record("https://exa.ai/")

        assert first.novel is True
        assert second.novel is False
        assert second.registrable_domain == "exa.ai"


class TestWindowExpiry:
    @pytest.mark.asyncio
    async def test_expired_sighting_is_novel_again(self, tmp_path: Path) -> None:
        cache_path = tmp_path / "egress_novelty_seen.json"
        stale_time = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        cache_path.write_text(json.dumps({"stale-domain.example": stale_time}))

        tracker = NovelDestinationTracker(cache_path=cache_path, window_seconds=14 * 86400.0)
        result = await tracker.check_and_record("https://stale-domain.example/page")

        assert result.novel is True


class TestCorruptCache:
    @pytest.mark.asyncio
    async def test_corrupt_cache_file_treated_as_empty(self, tmp_path: Path) -> None:
        cache_path = tmp_path / "egress_novelty_seen.json"
        cache_path.write_text("{not valid json")

        tracker = NovelDestinationTracker(cache_path=cache_path)
        result = await tracker.check_and_record("https://anything.example/page")

        assert result.novel is True

    @pytest.mark.asyncio
    async def test_missing_cache_file_treated_as_empty(self, tmp_path: Path) -> None:
        tracker = NovelDestinationTracker(cache_path=tmp_path / "does-not-exist.json")
        result = await tracker.check_and_record("https://anything.example/page")
        assert result.novel is True


class TestSaveFailureFailsOpen:
    @pytest.mark.asyncio
    async def test_unwritable_cache_path_does_not_raise(self, tmp_path: Path) -> None:
        """A save failure (e.g. permission error) must never surface as an exception —
        this is an observe-only signal, not allowed to break fetch_url.
        """
        blocked_dir = tmp_path / "blocked"
        blocked_dir.write_text("not a directory")  # a file where a directory is expected
        cache_path = blocked_dir / "egress_novelty_seen.json"

        tracker = NovelDestinationTracker(cache_path=cache_path)
        result = await tracker.check_and_record("https://anything.example/page")

        assert isinstance(result, NoveltyResult)
        assert result.novel is True


class TestConcurrency:
    @pytest.mark.asyncio
    async def test_concurrent_first_sightings_emit_exactly_once(self, tmp_path: Path) -> None:
        tracker = _tracker(tmp_path)
        results = await asyncio.gather(
            *[tracker.check_and_record("https://race-condition.example/page") for _ in range(5)]
        )

        novel_count = sum(1 for r in results if r.novel)
        assert novel_count == 1


class TestNoveltyResult:
    def test_is_frozen(self) -> None:
        r = NoveltyResult(novel=True, registrable_domain="example.com")
        with pytest.raises(Exception):
            r.novel = False  # type: ignore[misc]
