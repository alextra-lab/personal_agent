"""Tests for the delivery-ratio monitor CLI (FRE-1051).

Added because the CLI had no coverage at all, and it is the surface a standing monitor
actually runs. Three of its behaviours are load-bearing for a monitor and were each
broken or unasserted: ``--help`` under ``--json`` exited 0 with empty stdout, the
window guard was implemented twice in parallel branches, and a substrate failure was
indistinguishable by exit code from a real delivery breach.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

from personal_agent.observability.delivery_ratio.probe import FamilyDelivery, compute_report
from scripts.monitors import delivery_ratio_monitor as cli


class TestParseDay:
    """Window arguments are calendar days, and a bad one must be rejected loudly."""

    def test_parses_an_iso_day(self) -> None:
        assert cli._parse_day("2026-07-23") == date(2026, 7, 23)

    @pytest.mark.parametrize("bad", ["23-07-2026", "2026-7-23-", "yesterday", "", "2026-13-01"])
    def test_rejects_anything_else(self, bad: str) -> None:
        with pytest.raises(argparse.ArgumentTypeError):
            cli._parse_day(bad)


class TestWindowGuard:
    """An inverted window is a usage error, not an empty result.

    The guard used to be implemented separately in the --json and text branches, so the
    two could drift. It is asserted here in both modes against one implementation.
    """

    @pytest.mark.parametrize("extra", [[], ["--json"]])
    def test_inverted_window_exits_usage_without_measuring(
        self, extra: list[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        called = False

        async def _never(_args: object) -> object:
            nonlocal called
            called = True
            return 0

        monkeypatch.setattr(cli, "_measure", _never)
        code = cli.main(["--since", "2026-07-28", "--until", "2026-07-23", *extra])
        assert code == cli._EX_USAGE
        assert called is False


class TestProbeFailureIsDistinctFromBreach:
    """Exit 70 means "could not measure"; exit 1 means "delivery is bad".

    Collapsing them onto 1 would make an unreachable database look like a telemetry
    outage, sending triage to the wrong system.
    """

    @pytest.mark.parametrize("extra", [[], ["--json"]])
    def test_substrate_error_exits_seventy(
        self, extra: list[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _boom(_args: object) -> object:
            raise ConnectionRefusedError("postgres is down")

        monkeypatch.setattr(cli, "_measure", _boom)
        assert cli.main(["--since", "2026-07-23", "--until", "2026-07-23", *extra]) == 70

    def test_seventy_is_not_the_breach_code(self) -> None:
        assert cli._EX_PROBE_FAILED != 1


class TestHelpReachesRealStdout:
    """``--help`` must print to stdout and not be swallowed by the JSON redirect.

    Previously the help text was written to the diverted descriptor, so
    ``--json --help`` exited 0 with an empty stdout — which a wrapper capturing stdout
    and gating on the exit code scores as a silent success.
    """

    def test_json_help_writes_usage_to_a_real_stdout(self) -> None:
        """Runs a real subprocess, because in-process capture cannot see this bug.

        The defect is an ``os.dup2`` swap of descriptor 1. Both ``capsys`` (Python-level)
        and ``capfd`` (pytest's own fd substitution) pass with the defect reinstated —
        verified by applying that mutation and watching them stay green. Only a genuine
        child process with genuine descriptors distinguishes the two behaviours, so this
        asserts on real bytes: previously 0 on stdout with the usage on stderr.
        """
        proc = subprocess.run(
            [sys.executable, "-m", "scripts.monitors.delivery_ratio_monitor", "--json", "--help"],
            cwd=Path(__file__).resolve().parents[2],
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        assert proc.returncode == 0
        assert "delivery-ratio-monitor" in proc.stdout
        assert proc.stdout.strip() != ""


class TestEmit:
    """``_emit`` renders the report, or forwards a precondition code untouched."""

    def test_int_outcome_is_passed_through(self) -> None:
        assert cli._emit(cli._EX_USAGE, as_json=False) == cli._EX_USAGE

    def test_emit_writes_nothing_but_the_json_document(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Scoped to ``_emit``'s own writes, and drains the buffer first.

        Parsing the whole captured buffer made this order-dependent: it passed only when
        an earlier test had already triggered the config import, because otherwise
        conftest's setup logging landed in the same buffer and broke the parse. Run
        alone, it failed. The property worth asserting is that ``_emit`` itself writes
        exactly one JSON document; process-wide stdout cleanliness is covered by the
        subprocess test above.
        """
        report = compute_report(
            since=date(2026, 7, 23),
            until=date(2026, 7, 23),
            families=[
                FamilyDelivery(
                    family="api_cost_recorded",
                    oracle="postgres:api_costs",
                    oracle_count=144,
                    es_count=25,
                )
            ],
        )
        capsys.readouterr()  # discard setup/import noise; measure only _emit's writes
        assert cli._emit(report, as_json=True) == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "breach"
        assert payload["families"][0]["lost"] == 119
