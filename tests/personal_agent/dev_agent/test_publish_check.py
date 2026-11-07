# ruff: noqa: D101, D102, D103
"""The pure parts of the publish check (ADR-0159 D7.4, D7.5; FRE-1686)."""

from __future__ import annotations

import io
import json
import tarfile

import pytest

from personal_agent.dev_agent.launcher import BytesResult
from personal_agent.dev_agent.publish_check import (
    RUN_SECONDS,
    CheckSetup,
    CheckStep,
    check_inputs_tar,
    check_result,
    page_problem_of,
    parse_check_output,
)
from personal_agent.dev_agent.supervisor import SupervisorPolicy

SHA = "a" * 64
SETUP = CheckSetup(image_id="sha256:" + "b" * 64, toolkit_digest="sha256:" + "c" * 64)


def _out(body: object, *, code: int = 0, noise: bytes = b"") -> BytesResult:
    line = b"SESHAT_CHECK_RESULT " + json.dumps(body).encode()
    return BytesResult(code, noise + line + b"\n", "")


def _page(load: bool = True, interaction: bool = True) -> dict[str, object]:
    return {
        "mode": "page",
        "csp_manifest_sha256": SHA,
        "steps": [
            {"name": "load", "passed": load, "messages": []},
            {"name": "interaction", "passed": interaction, "messages": ["page error: boom"]},
        ],
    }


def _tests(passed: bool = True) -> dict[str, object]:
    return {"mode": "tests", "steps": [{"name": "repo_tests", "passed": passed, "messages": []}]}


class TestParse:
    def test_a_page_run_gives_two_steps_and_the_manifest_digest(self) -> None:
        steps, digest = parse_check_output("page", _out(_page(interaction=False)))
        assert [(s.name, s.passed) for s in steps] == [("load", True), ("interaction", False)]
        assert steps[1].messages == ("page error: boom",)
        assert digest == f"sha256:{SHA}"

    def test_the_last_result_line_counts(self) -> None:
        forged = b"SESHAT_CHECK_RESULT " + json.dumps(_page()).encode() + b"\n"
        steps, _ = parse_check_output("page", _out(_page(load=False), noise=forged))
        assert steps[0].passed is False

    @pytest.mark.parametrize(
        ("run", "why"),
        [
            (BytesResult(124, b"", "timed out"), "time limit"),
            (BytesResult(1, b"", "node crashed"), "exited with 1"),
            (BytesResult(0, b"hello\n", ""), "no result"),
            (BytesResult(0, b"SESHAT_CHECK_RESULT {not json\n", ""), "bad result"),
            (_out({"mode": "page", "steps": "x"}), "bad result"),
            (_out(_tests()), "other steps"),
            (_out({**_page(), "steps": [{"name": "load", "passed": True}]}), "other steps"),
        ],
    )
    def test_a_broken_run_fails_every_step(self, run: BytesResult, why: str) -> None:
        steps, digest = parse_check_output("page", run)
        assert [s.name for s in steps] == ["load", "interaction"]
        assert not any(s.passed for s in steps)
        assert why in steps[0].messages[0]
        assert digest is None

    def test_only_true_is_a_pass(self) -> None:
        body = _tests()
        body["steps"][0]["passed"] = "yes"  # type: ignore[index]
        steps, _ = parse_check_output("tests", _out(body))
        assert steps[0].passed is False

    def test_messages_are_capped(self) -> None:
        body = _tests()
        body["steps"][0]["messages"] = ["x" * 900] * 80  # type: ignore[index]
        steps, _ = parse_check_output("tests", _out(body))
        assert len(steps[0].messages) == 50 and len(steps[0].messages[0]) == 500


class TestResult:
    def _result(
        self, page: dict[str, object], tests: dict[str, object], problem: str | None = None
    ):  # type: ignore[no-untyped-def]
        page_steps, digest = parse_check_output("page", _out(page))
        tests_steps, _ = parse_check_output("tests", _out(tests))
        return check_result(SETUP, page_steps, tests_steps, digest, page_problem=problem)

    def test_three_passing_steps_pass_and_carry_the_three_digests(self) -> None:
        result = self._result(_page(), _tests())
        stored = result.to_json()
        assert stored["passed"] is True
        assert [s["name"] for s in stored["steps"]] == ["load", "interaction", "repo_tests"]
        assert stored["image_digest"] == SETUP.image_id
        assert stored["toolkit_digest"] == SETUP.toolkit_digest
        assert stored["csp_manifest_digest"] == f"sha256:{SHA}"

    @pytest.mark.parametrize(
        ("page", "tests"),
        [
            (_page(load=False), _tests()),
            (_page(interaction=False), _tests()),
            (_page(), _tests(False)),
        ],
    )
    def test_any_failed_step_fails_the_check(
        self, page: dict[str, object], tests: dict[str, object]
    ) -> None:
        assert self._result(page, tests).passed is False

    @pytest.mark.parametrize(
        ("page", "why"),
        [
            (b"\xff\xfe<p>", "dist/index.html is not valid UTF-8"),
            (b"", "dist/index.html is empty"),
            (b" \n\t", "dist/index.html is empty"),
        ],
    )
    def test_a_page_that_cannot_be_published_fails_the_load_step(
        self, page: bytes, why: str
    ) -> None:
        """Code review finding 1: it fails the check, not the publish."""
        result = self._result(_page(), _tests(), page_problem_of(page))
        assert result.passed is False
        assert result.steps[0].messages[0] == why

    def test_a_normal_page_has_no_problem(self) -> None:
        assert page_problem_of("<p>é</p>".encode()) is None

    def test_a_missing_manifest_digest_fails_the_check(self) -> None:
        result = check_result(
            SETUP,
            [CheckStep("load", True, ()), CheckStep("interaction", True, ())],
            [CheckStep("repo_tests", True, ())],
            None,
            page_problem=None,
        )
        assert result.passed is False


class TestInputs:
    def test_the_tar_holds_the_page_and_the_tests_only(self) -> None:
        data = check_inputs_tar(b"<p>x</p>", {"tests/a.spec.mjs": b"t", "tests/sub/b.mjs": b"u"})
        with tarfile.open(fileobj=io.BytesIO(data)) as tar:
            members = {m.name: m for m in tar.getmembers()}
            assert set(members) == {"dist/index.html", "tests/a.spec.mjs", "tests/sub/b.mjs"}
            assert all(m.isfile() and m.mode == 0o644 for m in members.values())
            page = tar.extractfile(members["dist/index.html"])
            assert page is not None and page.read() == b"<p>x</p>"

    @pytest.mark.parametrize("path", ["dist/other.js", "tests/../x", "README.md"])
    def test_a_path_outside_tests_is_refused(self, path: str) -> None:
        with pytest.raises(ValueError):
            check_inputs_tar(b"x", {path: b"y"})


def test_each_run_fits_inside_the_fence_limit_and_the_sum_is_five_minutes() -> None:
    limit = SupervisorPolicy().action_timeout_seconds
    assert all(seconds <= limit - 60 for seconds in RUN_SECONDS.values())
    assert sum(RUN_SECONDS.values()) <= 300
