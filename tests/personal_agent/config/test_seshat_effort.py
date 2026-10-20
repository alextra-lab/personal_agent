"""FRE-1622 (ADR-0157 D1, D3): the Seshat effort presets and their start-up refusals (AC-1)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import yaml
from personal_agent.config.seshat_effort import (
    EffortSpec,
    SeshatEffortConfigError,
    effort_spec,
    get_current_effort,
    load_seshat_effort,
    reset_current_effort,
    rounds_for_turn,
    set_current_effort,
)

from personal_agent.config import get_settings

_VALID = {
    "default_level": "standard",
    "levels": {
        "fast": {
            "expansion": False,
            "sub_agent_rounds": None,
            "planner_policy": None,
            "primary_mode": "fast",
        },
        "standard": {
            "expansion": True,
            "sub_agent_rounds": 12,
            "planner_policy": "p2",
            "primary_mode": None,
        },
        "deep": {
            "expansion": True,
            "sub_agent_rounds": 20,
            "planner_policy": "baseline",
            "primary_mode": "deep",
        },
    },
}


def _write(tmp_path: Path, data: object) -> Path:
    path = tmp_path / "seshat_effort.yaml"
    path.write_text(yaml.safe_dump(data))
    return path


def _settings(**overrides: object) -> object:
    return get_settings().model_copy(update=overrides)


def _variant(**level_overrides: dict[str, object]) -> dict[str, object]:
    levels = {name: dict(values) for name, values in _VALID["levels"].items()}  # type: ignore[attr-defined]
    for name, values in level_overrides.items():
        levels[name] = {**levels.get(name, {}), **values}
    return {**_VALID, "levels": levels}


class TestTheCommittedFile:
    def test_it_loads_with_the_adr_values(self) -> None:
        config = load_seshat_effort(
            settings=_settings(
                sub_agent_max_tool_iterations=20, sub_agent_rounds_by_thoroughness={}
            )
        )
        assert config.default_level == "standard"
        fast, standard, deep = (config.levels[n] for n in ("fast", "standard", "deep"))
        assert (fast.expansion, fast.sub_agent_rounds, fast.primary_mode) == (False, None, "fast")
        assert (standard.expansion, standard.sub_agent_rounds, standard.primary_mode) == (
            True,
            12,
            None,
        )
        assert standard.planner_policy in ("p2", "baseline")
        assert (deep.expansion, deep.sub_agent_rounds, deep.planner_policy, deep.primary_mode) == (
            True,
            20,
            "baseline",
            "deep",
        )


class TestTheRefusals:
    def test_a_valid_file_loads(self, tmp_path: Path) -> None:
        config = load_seshat_effort(_write(tmp_path, _VALID), settings=_settings())
        assert config.levels["deep"].level == "deep"

    def test_rounds_above_the_ceiling_refuse(self, tmp_path: Path) -> None:
        path = _write(tmp_path, _VALID)
        with pytest.raises(SeshatEffortConfigError, match="sub_agent_max_tool_iterations"):
            load_seshat_effort(path, settings=_settings(sub_agent_max_tool_iterations=12))

    def test_a_thoroughness_map_refuses(self, tmp_path: Path) -> None:
        path = _write(tmp_path, _VALID)
        with pytest.raises(SeshatEffortConfigError, match="sub_agent_rounds_by_thoroughness"):
            load_seshat_effort(
                path, settings=_settings(sub_agent_rounds_by_thoroughness={"quick": 5})
            )

    @pytest.mark.parametrize(
        "data",
        [
            pytest.param({**_VALID, "default_level": "turbo"}, id="unknown-default-level"),
            pytest.param(
                {**_VALID, "levels": {**_VALID["levels"], "turbo": _VALID["levels"]["deep"]}},  # type: ignore[dict-item]
                id="unknown-level",
            ),
            pytest.param(
                {
                    **_VALID,
                    "levels": {k: v for k, v in _VALID["levels"].items() if k != "deep"},  # type: ignore[attr-defined]
                },
                id="missing-level",
            ),
            pytest.param(_variant(fast={"primary_mode": "ultra"}), id="unknown-mode"),
            pytest.param(_variant(deep={"planner_policy": "p3"}), id="unknown-policy"),
            pytest.param(_variant(standard={"sub_agent_rounds": None}), id="expansion-no-rounds"),
            pytest.param(_variant(standard={"planner_policy": None}), id="expansion-no-policy"),
            pytest.param(_variant(standard={"sub_agent_rounds": 0}), id="zero-rounds"),
            pytest.param(_variant(deep={"thinking": True}), id="unknown-value"),
        ],
    )
    def test_an_unknown_or_incomplete_value_refuses(
        self, tmp_path: Path, data: dict[str, object]
    ) -> None:
        with pytest.raises(SeshatEffortConfigError):
            load_seshat_effort(_write(tmp_path, data), settings=_settings())

    def test_a_missing_file_refuses(self, tmp_path: Path) -> None:
        with pytest.raises(SeshatEffortConfigError):
            load_seshat_effort(tmp_path / "absent.yaml", settings=_settings())


class TestThePerTurnSpec:
    def test_without_a_set_spec_the_default_level_applies(self) -> None:
        assert get_current_effort() == effort_spec(None)
        assert get_current_effort().level == "standard"

    def test_a_set_spec_is_read_back_and_reset(self) -> None:
        token = set_current_effort(effort_spec("deep"))
        try:
            assert get_current_effort().level == "deep"
        finally:
            reset_current_effort(token)
        assert get_current_effort().level == "standard"

    def test_the_spec_is_frozen(self) -> None:
        spec = effort_spec("deep")
        with pytest.raises(Exception):  # noqa: B017 - Pydantic's frozen error type
            spec.sub_agent_rounds = 3  # type: ignore[misc]

    def test_a_task_keeps_the_spec_it_launched_with(self) -> None:
        """ADR-0079 invariant 7: a change after launch does not reach the running turn."""

        async def scenario() -> tuple[str, str]:
            started = asyncio.Event()
            release = asyncio.Event()

            async def turn() -> str:
                started.set()
                await release.wait()
                return get_current_effort().level

            set_current_effort(effort_spec("fast"))
            task = asyncio.create_task(turn())
            await started.wait()
            set_current_effort(effort_spec("deep"))
            release.set()
            return await task, get_current_effort().level

        assert asyncio.run(scenario()) == ("fast", "deep")

    @pytest.mark.parametrize(("level", "rounds"), [("standard", 12), ("deep", 20)])
    def test_rounds_for_turn_reads_the_spec_not_the_ceiling(
        self, monkeypatch: pytest.MonkeyPatch, level: str, rounds: int
    ) -> None:
        monkeypatch.setattr(get_settings(), "sub_agent_max_tool_iterations", 30)
        token = set_current_effort(effort_spec(level))  # type: ignore[arg-type]
        try:
            assert {rounds_for_turn(t) for t in ("quick", "standard", "thorough")} == {rounds}
        finally:
            reset_current_effort(token)

    def test_rounds_for_turn_falls_back_to_the_setting_for_a_level_without_rounds(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(get_settings(), "sub_agent_max_tool_iterations", 30)
        token = set_current_effort(effort_spec("fast"))
        try:
            assert rounds_for_turn("standard") == 30
        finally:
            reset_current_effort(token)

    def test_effort_spec_is_an_effort_spec(self) -> None:
        assert isinstance(effort_spec("fast"), EffortSpec)
