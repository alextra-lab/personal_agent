"""Tests for the D-mechanism cache reset cadence monitor (ADR-0092 §D7, FRE-572)."""

from __future__ import annotations

import math
from unittest.mock import patch

import pytest

from personal_agent.orchestrator.executor import _emit_cadence_monitor_doc


class TestEmitCadenceMonitorDoc:
    def test_calls_schedule_es_index(self) -> None:
        with patch("personal_agent.captains_log.es_indexer.schedule_es_index") as mock_idx:
            _emit_cadence_monitor_doc(
                trace_id="t-1",
                session_id="s-1",
                backend="llamacpp",
                actual_turns=8,
                optimal_run_length=5.66,
                reason="optimum",
            )
            mock_idx.assert_called_once()

    def test_doc_contains_required_fields(self) -> None:
        captured: list[dict] = []

        def _fake_idx(index_name: str, doc: dict, **kwargs: object) -> None:
            captured.append({"index_name": index_name, **doc})

        with patch(
            "personal_agent.captains_log.es_indexer.schedule_es_index", side_effect=_fake_idx
        ):
            _emit_cadence_monitor_doc(
                trace_id="t-2",
                session_id="s-2",
                backend="llamacpp",
                actual_turns=10,
                optimal_run_length=8.0,
                reason="optimum",
            )

        assert len(captured) == 1
        doc = captured[0]
        assert doc["trace_id"] == "t-2"
        assert doc["session_id"] == "s-2"
        assert doc["backend"] == "llamacpp"
        assert doc["actual_turns"] == 10
        assert doc["l_star"] == pytest.approx(8.0)
        assert doc["reason"] == "optimum"
        assert "@timestamp" in doc

    def test_deviation_turns_computed(self) -> None:
        captured: list[dict] = []

        def _fake_idx(index_name: str, doc: dict, **kwargs: object) -> None:
            captured.append(doc)

        with patch(
            "personal_agent.captains_log.es_indexer.schedule_es_index", side_effect=_fake_idx
        ):
            _emit_cadence_monitor_doc(
                trace_id="t-3",
                session_id="s-3",
                backend="llamacpp",
                actual_turns=10,
                optimal_run_length=8.0,
                reason="optimum",
            )

        doc = captured[0]
        # deviation = actual_turns - l_star = 10 - 8.0 = 2.0
        assert doc["deviation_turns"] == pytest.approx(2.0)

    def test_l_star_is_none_when_infinite(self) -> None:
        captured: list[dict] = []

        def _fake_idx(index_name: str, doc: dict, **kwargs: object) -> None:
            captured.append(doc)

        with patch(
            "personal_agent.captains_log.es_indexer.schedule_es_index", side_effect=_fake_idx
        ):
            _emit_cadence_monitor_doc(
                trace_id="t-4",
                session_id="s-4",
                backend="llamacpp",
                actual_turns=5,
                optimal_run_length=math.inf,
                reason="token_ceiling",
            )

        doc = captured[0]
        assert doc["l_star"] is None

    def test_deviation_turns_is_none_when_l_star_infinite(self) -> None:
        captured: list[dict] = []

        def _fake_idx(index_name: str, doc: dict, **kwargs: object) -> None:
            captured.append(doc)

        with patch(
            "personal_agent.captains_log.es_indexer.schedule_es_index", side_effect=_fake_idx
        ):
            _emit_cadence_monitor_doc(
                trace_id="t-5",
                session_id="s-5",
                backend="llamacpp",
                actual_turns=5,
                optimal_run_length=math.inf,
                reason="token_ceiling",
            )

        doc = captured[0]
        assert doc["deviation_turns"] is None

    def test_doc_id_keyed_by_trace(self) -> None:
        call_kwargs: list[dict] = []

        def _fake_idx(
            index_name: str, doc: dict, doc_id: str | None = None, **kwargs: object
        ) -> None:
            call_kwargs.append({"doc_id": doc_id})

        with patch(
            "personal_agent.captains_log.es_indexer.schedule_es_index", side_effect=_fake_idx
        ):
            _emit_cadence_monitor_doc(
                trace_id="t-6",
                session_id="s-6",
                backend="llamacpp",
                actual_turns=6,
                optimal_run_length=5.0,
                reason="optimum",
            )

        assert call_kwargs[0]["doc_id"] == "t-6:D"

    def test_index_name_includes_date(self) -> None:
        index_names: list[str] = []

        def _fake_idx(index_name: str, doc: dict, **kwargs: object) -> None:
            index_names.append(index_name)

        with patch(
            "personal_agent.captains_log.es_indexer.schedule_es_index", side_effect=_fake_idx
        ):
            _emit_cadence_monitor_doc(
                trace_id="t-7",
                session_id="s-7",
                backend="llamacpp",
                actual_turns=7,
                optimal_run_length=6.0,
                reason="optimum",
            )

        assert index_names[0].startswith("agent-monitors-cache-reset-cadence-")
