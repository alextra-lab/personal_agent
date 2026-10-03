# ruff: noqa: D103
"""FRE-1542 — an eval bring-up must never put a production service in its compose plan.

On 2026-10-03 `docker compose -p seshat ... up -d <eval services>` run from a worktree
recreated production's `cloud-sim-searxng`: `depends_on` pulls the gateways' dependencies
into scope, and `required: false` (FRE-1344) did not stop that. `make eval-infra-up` now runs
`up --no-deps` and checks a `--dry-run` of the same command with `compose_plan_guard` first.

The plan text below is captured from real `docker compose up --dry-run` runs on this host.
"""

from __future__ import annotations

import subprocess

import pytest
from scripts.eval import compose_plan_guard as guard

from personal_agent.config.config_guard import repo_root

# `docker compose -p seshat -f cloud -f eval up -d --build --dry-run <eval services>` from a
# worktree, on the source before FRE-1542 (trimmed to the Container lines).
_PLAN_BEFORE_THE_FIX = """\
 Container cloud-sim-redis-eval Running
 Container cloud-sim-postgres-eval Recreate
 Container cloud-sim-searxng Recreate
 Container cloud-sim-searxng Recreated
 Container cloud-sim-postgres-eval Recreated
 Container cloud-sim-seshat-gateway-treatment Creating
 Container cloud-sim-seshat-gateway-control Recreate
 Container cloud-sim-seshat-gateway-control Recreated
 Container ac2df444474a_cloud-sim-searxng Starting
 Container ac2df444474a_cloud-sim-searxng Started
 Container cloud-sim-postgres-eval Waiting
 Container cloud-sim-postgres-eval Healthy
"""

# The same invocation with `--no-deps`: only the named eval services appear.
_PLAN_AFTER_THE_FIX = """\
 Container cloud-sim-postgres-eval Recreate
 Container cloud-sim-postgres-eval Recreated
 Container 2948755a0d26_cloud-sim-postgres-eval Starting
 Container 2948755a0d26_cloud-sim-postgres-eval Started
 Container cloud-sim-seshat-gateway-control Recreate
 Container cloud-sim-seshat-gateway-control Recreated
 Container cloud-sim-seshat-gateway-treatment Creating
 Container cloud-sim-seshat-gateway-treatment Created
 Container cloud-sim-redis-eval Running
"""

_EVAL_CONTAINERS = frozenset(
    {
        "cloud-sim-postgres-eval",
        "cloud-sim-neo4j-eval",
        "cloud-sim-elasticsearch-eval",
        "cloud-sim-redis-eval",
        "cloud-sim-seshat-gateway-control",
        "cloud-sim-seshat-gateway-treatment",
    }
)


class TestPlanGuard:
    def test_refuses_a_plan_that_recreates_a_production_service(self) -> None:
        """AC-4: the plan that took production search down on 2026-10-03 is refused."""
        violations = guard.production_containers_in(_PLAN_BEFORE_THE_FIX, _EVAL_CONTAINERS)
        assert violations == ["cloud-sim-searxng"]

    def test_accepts_a_plan_that_names_only_eval_services(self) -> None:
        """AC-2: after `--no-deps`, the plan holds no production service."""
        assert guard.production_containers_in(_PLAN_AFTER_THE_FIX, _EVAL_CONTAINERS) == []

    def test_a_recreate_temporary_name_does_not_hide_the_service(self) -> None:
        """Compose names the replacement `<12 hex>_<name>`; the prefix must not disguise it."""
        plan = " Container ac2df444474a_cloud-sim-searxng Starting\n"
        assert guard.production_containers_in(plan, _EVAL_CONTAINERS) == ["cloud-sim-searxng"]

    def test_a_production_container_is_refused_even_when_only_running(self) -> None:
        """A service in scope at all is a dependency pulled in; `--no-deps` must leave none."""
        plan = " Container cloud-sim-searxng Running\n"
        assert guard.production_containers_in(plan, _EVAL_CONTAINERS) == ["cloud-sim-searxng"]

    def test_an_eval_name_that_extends_a_production_name_is_still_eval(self) -> None:
        """`cloud-sim-postgres-eval` is eval; `cloud-sim-postgres` is production."""
        plan = (
            " Container cloud-sim-postgres Stopping\n Container cloud-sim-postgres-eval Stopping\n"
        )
        assert guard.production_containers_in(plan, _EVAL_CONTAINERS) == ["cloud-sim-postgres"]

    def test_an_empty_plan_is_refused_not_passed(self) -> None:
        """No Container line means the dry-run failed, not that the plan is clean."""
        with pytest.raises(guard.PlanGuardError, match="no container"):
            guard.check_plan("", _EVAL_CONTAINERS)
        with pytest.raises(guard.PlanGuardError, match="no container"):
            guard.check_plan("error during connect: daemon not running\n", _EVAL_CONTAINERS)

    def test_a_container_line_in_an_unreadable_shape_is_refused_not_skipped(self) -> None:
        """A format change must fail closed: one unread production line would pass the rest."""
        plan = (
            " Container cloud-sim-postgres-eval Recreate\n"
            " Container cloud-sim-searxng Stopped 0.2s\n"
        )
        with pytest.raises(guard.PlanGuardError, match="cannot read"):
            guard.check_plan(plan, _EVAL_CONTAINERS)

    def test_a_down_of_a_stopped_stack_may_have_an_empty_plan(self) -> None:
        guard.check_plan("", _EVAL_CONTAINERS, allow_empty=True)
        with pytest.raises(guard.PlanGuardError, match="cloud-sim-searxng"):
            guard.check_plan(
                " Container cloud-sim-searxng Stopping\n", _EVAL_CONTAINERS, allow_empty=True
            )

    def test_check_plan_names_the_offending_service_and_the_fix(self) -> None:
        with pytest.raises(guard.PlanGuardError) as excinfo:
            guard.check_plan(_PLAN_BEFORE_THE_FIX, _EVAL_CONTAINERS)
        assert "cloud-sim-searxng" in str(excinfo.value)
        assert "--no-deps" in str(excinfo.value)

    def test_main_exits_nonzero_on_a_refused_plan(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        import io

        monkeypatch.setattr("sys.stdin", io.StringIO(_PLAN_BEFORE_THE_FIX))
        assert guard.main([]) == 1
        assert "cloud-sim-searxng" in capsys.readouterr().err

    def test_main_exits_zero_on_a_clean_plan(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import io

        monkeypatch.setattr("sys.stdin", io.StringIO(_PLAN_AFTER_THE_FIX))
        assert guard.main([]) == 0


class TestEvalContainerSet:
    def test_is_read_from_the_eval_compose_file(self) -> None:
        """The allowed set is the eval file's own services, so a new eval service needs no edit."""
        names = guard.eval_container_names(repo_root() / "docker-compose.eval.yml")
        assert names == _EVAL_CONTAINERS
        assert "cloud-sim-searxng" not in names


class TestMakeTargets:
    @staticmethod
    def _recipe(target: str) -> str:
        result = subprocess.run(
            ["make", "-n", target],
            cwd=repo_root(),
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout

    def test_eval_infra_up_pins_project_names_no_deps_and_guards_before_acting(self) -> None:
        recipe = self._recipe("eval-infra-up")
        assert "-p seshat " in recipe, "project must not follow the working directory's name"
        assert recipe.count("--no-deps") >= 3, "dry-run and both real phases must use --no-deps"
        assert "--dry-run" in recipe
        assert "compose_plan_guard" in recipe
        dry_run = recipe.index("--dry-run")
        guard_call = recipe.index("compose_plan_guard")
        first_real_up = recipe.index("up -d --no-deps --wait")
        assert dry_run < guard_call < first_real_up

    def test_eval_infra_up_starts_substrate_healthy_before_the_gateways(self) -> None:
        """`--no-deps` drops compose's `service_healthy` ordering; the target must restore it."""
        recipe = self._recipe("eval-infra-up")
        substrate = recipe.index("up -d --no-deps --wait postgres-eval")
        gateways = recipe.index("up -d --no-deps --build seshat-gateway-control")
        assert substrate < gateways

    def test_eval_infra_down_is_guarded_too(self) -> None:
        recipe = self._recipe("eval-infra-down")
        assert "--allow-empty-plan" in recipe, "a stopped stack must stay a no-op, not a refusal"
        assert "-p seshat " in recipe
        assert recipe.index("--dry-run") < recipe.index("compose_plan_guard")
        assert "compose_plan_guard" in recipe
