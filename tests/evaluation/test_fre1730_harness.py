"""FRE-1730: the parts of the replay harness that need no ``claude`` process."""

from __future__ import annotations

import dataclasses
import json
import os
import re
import socket
import subprocess
import threading
from pathlib import Path

import pytest
from scripts.eval.fre1730 import report, run, score, session, tasks
from scripts.eval.fre1730.egress import EgressPolicy, serve_gate
from scripts.eval.fre1730.footprint import (
    compare,
    plugin_changed_content,
    plugin_only,
    snapshot,
)
from scripts.eval.fre1730.jail import Jail
from scripts.eval.fre1730.matrix import _stop_reason, is_valid, plan_sessions
from scripts.eval.fre1730.spend import RunStopped, SpendStop
from scripts.eval.fre1730.transcript import TranscriptFacts, arm_problems, read_transcript

_ALL_ON = frozenset({"User", "Project"})


def _transcript(path: Path, sent: str, files: list[dict[str, str]]) -> Path:
    lines = [
        {"type": "attachment", "attachment": {"type": "instructions", "files": files}},
        {"type": "api-request-blob", "message": {"role": "user", "content": sent}},
        {"type": "api-request"},
    ]
    path.write_text("\n".join(json.dumps(line) for line in lines))
    return path


def test_the_reader_finds_what_reached_the_model(tmp_path: Path) -> None:
    files = [
        {"type": "User", "content": "global rules"},
        {"type": "Project", "content": 'project "rules"\nline two'},
    ]
    sent = 'global rules project "rules"\nline two MARK'
    facts = read_transcript(_transcript(tmp_path / "t.jsonl", sent, files), "MARK")
    assert facts == TranscriptFacts(True, _ALL_ON, 1)
    assert arm_problems(facts, "plugin") == []


def test_a_claude_md_that_was_listed_but_not_sent_does_not_count(tmp_path: Path) -> None:
    files = [{"type": "User", "content": "global rules"}, {"type": "Project", "content": "gone"}]
    facts = read_transcript(_transcript(tmp_path / "t.jsonl", "global rules", files), "MARK")
    assert facts.instructions_sent == {"User"}
    assert arm_problems(facts, "control") == [
        "control arm: the Project CLAUDE.md did not reach the model"
    ]


@pytest.mark.parametrize(
    ("facts", "arm", "problem"),
    [
        (TranscriptFacts(False, _ALL_ON, 1), "plugin", "the marker did not reach"),
        (TranscriptFacts(True, _ALL_ON, 1), "control", "the marker reached"),
        (TranscriptFacts(True, frozenset(), 1), "plugin", "CLAUDE.md did not reach"),
        (TranscriptFacts(False, _ALL_ON, 0), "control", "no request"),
    ],
)
def test_seeded_negatives_of_the_arm_check(facts: TranscriptFacts, arm: str, problem: str) -> None:
    assert problem in " ".join(arm_problems(facts, arm))  # type: ignore[arg-type]


def test_the_stop_uses_the_lower_of_the_owners_number_and_the_projection() -> None:
    assert SpendStop(hard_stop_usd=30, projected_usd=10).cap_usd == 15
    assert SpendStop(hard_stop_usd=30, projected_usd=25).cap_usd == 30


def test_the_stop_ends_a_run_at_the_cap() -> None:
    stop = SpendStop(hard_stop_usd=3, projected_usd=10)
    stop.add(2.0)
    stop.check()
    stop.add(1.0)
    with pytest.raises(RunStopped, match="reached the cap"):
        stop.check()


def test_the_session_budget_is_fixed_until_the_last_sessions() -> None:
    stop = SpendStop(hard_stop_usd=30, projected_usd=30)
    assert stop.session_budget_usd == 10
    stop.add(25.0)
    assert stop.session_budget_usd == 5


def test_the_footprint_reports_each_change(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / "same").write_text("a")
    (home / ".claude" / "edited").write_text("a")
    (home / ".claude" / "gone").write_text("a")
    before = snapshot(home)
    (home / ".claude" / "edited").write_text("b")
    (home / ".claude" / "gone").unlink()
    (home / ".config").mkdir()
    (home / ".config" / "new").write_text("c")
    change = compare(before, snapshot(home))
    assert change.lines() == [
        "added    .config/new",
        "changed  .claude/edited",
        "removed  .claude/gone",
    ]


def test_a_file_the_host_cannot_read_is_listed_and_does_not_raise(tmp_path: Path) -> None:
    locked = tmp_path / "locked"
    locked.write_text("x")
    locked.chmod(0)
    try:
        assert snapshot(tmp_path)["locked"].startswith("unreadable:")
    finally:
        locked.chmod(0o600)


def test_plugin_only_ignores_what_claude_code_writes_in_both_arms() -> None:
    plugin = [
        ".claude/projects/-tmp/11111111-2222-3333-4444-555555555555.jsonl",
        ".claude/backups/.claude.json.backup.1793799858878",
        ".npm/_logs/2026-11-04T14_05_03_675Z-debug-0.log",
        ".claude/.flag",
    ]
    control = [
        ".npm/_logs/2026-11-04T14_05_14_052Z-debug-0.log",
        ".claude/projects/-tmp/66666666-7777-8888-9999-000000000000.jsonl",
        ".claude/backups/.claude.json.backup.1793799999999",
    ]
    assert plugin_only(plugin, control) == (".claude/.flag",)


def test_the_plan_mixes_the_arms_and_is_repeatable() -> None:
    plan = plan_sessions(tasks.load_tasks(), 3, "seed")
    assert len(plan) == 30
    assert [s.key for s in plan] == [s.key for s in plan_sessions(tasks.load_tasks(), 3, "seed")]
    first_half = [s.arm for s in plan[:15]]
    assert first_half.count("plugin") not in (0, 15)


def _record(ticket: str, arm: str, value: float) -> dict[str, object]:
    return {"ticket": ticket, "arm": arm, "lines_code": value}


def test_a_difference_inside_the_noise_gap_does_not_count() -> None:
    records = [_record("T", "plugin", v) for v in (10, 20, 30)]
    records += [_record("T", "control", v) for v in (15, 25, 35)]
    row = report.compare_metric(records, "lines_code")[0]
    assert (row.difference, row.noise_gap, row.counts) == (-5, 20, False)


def test_a_difference_outside_the_noise_gap_counts() -> None:
    records = [_record("T", "plugin", v) for v in (10, 11, 12)]
    records += [_record("T", "control", v) for v in (30, 31, 32)]
    row = report.compare_metric(records, "lines_code")[0]
    assert (row.difference, row.noise_gap, row.counts) == (-20, 2, True)


def test_one_run_per_arm_measures_no_noise_and_never_counts() -> None:
    records = [_record("T", "plugin", 10), _record("T", "control", 100)]
    assert report.compare_metric(records, "lines_code")[0].counts is False


def test_numstat_splits_code_from_tests() -> None:
    numstat = "3\t1\tsrc/a.py\n10\t2\ttests/test_a.py\n-\t-\timage.png\n5\t0\tscripts/test_b.py\n"
    assert score.parse_numstat(numstat) == (4, 17)


@pytest.mark.parametrize(
    ("output", "exit_code", "expected"),
    [
        ("....\n4 passed in 1.2s\n", 0, (4, 0)),
        ("F.\n1 failed, 1 passed in 1.2s\n", 1, (1, 1)),
        ("E\n1 error in 0.2s\n", 2, (0, 1)),
        ("ImportError: nothing\n", 4, (0, 1)),
        ("", 5, (0, 1)),
        (
            "3 passed, 1 warning in 1.2s\nLiteLLM:INFO - flushed 14 events in 2 ms\nMessage: 'x'\n",
            0,
            (3, 0),
        ),
        ("=== 2 failed, 3 passed in 1.2s ===\n", 1, (3, 2)),
        ("no summary line in this output\n", 0, (0, 1)),
        ("7 deselected in 0.1s\n", 5, (0, 1)),
    ],
)
def test_pytest_output_scores(output: str, exit_code: int, expected: tuple[int, int]) -> None:
    result = score.parse_pytest(output, exit_code)
    assert (result.passed, result.failed) == expected


def _ask_gate(path: Path, request: bytes) -> bytes:
    with socket.socket(socket.AF_UNIX) as client:
        client.connect(str(path))
        client.sendall(request)
        return client.recv(4096)


def test_the_gate_refuses_a_host_that_is_not_allowed(tmp_path: Path) -> None:
    policy = EgressPolicy(allowed=frozenset({("api.anthropic.com", 443)}))
    path = tmp_path / "g.sock"
    with serve_gate(path, policy):
        answer = _ask_gate(path, b"CONNECT evil.example:443 HTTP/1.1\r\n\r\n")
        plain = _ask_gate(path, b"GET http://evil.example/ HTTP/1.1\r\n\r\n")
    assert b"403" in answer
    assert b"403" in plain
    assert policy.refused == ["CONNECT evil.example:443", "GET http://evil.example/"]


def test_the_gate_refuses_a_bad_port_and_records_it(tmp_path: Path) -> None:
    policy = EgressPolicy(allowed=frozenset({("api.anthropic.com", 443)}))
    path = tmp_path / "g.sock"
    with serve_gate(path, policy):
        answer = _ask_gate(path, b"CONNECT api.anthropic.com:abc HTTP/1.1\r\n\r\n")
    assert b"403" in answer
    assert policy.refused == ["CONNECT api.anthropic.com:abc"]


def test_the_gate_tunnels_an_allowed_host(tmp_path: Path) -> None:
    listener = socket.create_server(("127.0.0.1", 0))
    port = listener.getsockname()[1]

    def echo() -> None:
        conn, _ = listener.accept()
        with conn:
            conn.sendall(conn.recv(100).upper())

    thread = threading.Thread(target=echo, daemon=True)
    thread.start()
    policy = EgressPolicy(allowed=frozenset({("127.0.0.1", port)}))
    path = tmp_path / "g.sock"
    with serve_gate(path, policy), socket.socket(socket.AF_UNIX) as client:
        client.connect(str(path))
        client.sendall(f"CONNECT 127.0.0.1:{port} HTTP/1.1\r\n\r\n".encode())
        assert b"200" in client.recv(4096)
        client.sendall(b"hello")
        assert client.recv(100) == b"HELLO"
    thread.join(timeout=5)
    listener.close()
    assert policy.refused == []


def test_a_paid_run_refuses_no_go_and_no_number(capsys: pytest.CaptureFixture[str]) -> None:
    base = ["paid", "--plugin-dir", "x", "--marker", "m"]
    with pytest.raises(SystemExit):
        run.main([*base, "--projected-usd", "1", "--owner-go", "go"])
    assert "--hard-stop-usd" in capsys.readouterr().err
    with pytest.raises(SystemExit, match="--owner-go"):
        run.main([*base, "--hard-stop-usd", "1", "--projected-usd", "1", "--owner-go", " "])


def test_the_paid_route_takes_one_credential_for_this_run_only() -> None:
    with pytest.raises(SystemExit, match="exactly one"):
        session.paid_route({"AGENT_ANTHROPIC_API_KEY": "production"})
    with pytest.raises(SystemExit, match="exactly one"):
        session.paid_route({"FRE1730_CLAUDE_OAUTH_TOKEN": "a", "FRE1730_ANTHROPIC_API_KEY": "b"})
    route = session.paid_route({"FRE1730_CLAUDE_OAUTH_TOKEN": "a"})
    assert route.kind == "max-plan"
    assert route.env["CLAUDE_CODE_OAUTH_TOKEN"] == "a"
    assert "ANTHROPIC_BASE_URL" not in route.env


def test_each_task_has_a_prompt_with_no_deployment_identifier() -> None:
    loaded = tasks.load_tasks()
    assert len(loaded) == 5
    for task in loaded:
        prompt = task.prompt()
        assert task.ticket in prompt
        assert not re.search(r"linear\.app|github\.com|trace `|session `", prompt)
        assert "post-deploy" not in prompt.lower()


def test_the_plugin_hook_events_are_read_as_text() -> None:
    assert report.plugin_hook_events(run.FIXTURE_PLUGIN) == ["SessionStart"]


def test_a_transcript_link_that_leaves_the_home_does_not_count(tmp_path: Path) -> None:
    linked, real, pipe = (f"{n}1111111-2222-3333-4444-555555555555" for n in "abc")
    secret = tmp_path / "secret"
    secret.write_text("x")
    project = tmp_path / "home" / ".claude" / "projects" / "p"
    project.mkdir(parents=True)
    (project / f"{linked}.jsonl").symlink_to(secret)
    (project / f"{real}.jsonl").write_text("{}")
    os.mkfifo(project / f"{pipe}.jsonl")
    assert session.find_transcript(tmp_path / "home", linked) is None
    assert session.find_transcript(tmp_path / "home", pipe) is None
    assert session.find_transcript(tmp_path / "home", real) == project / f"{real}.jsonl"


def _plugin_clone(path: Path, url: str) -> str:
    path.mkdir()
    for args in (
        ("init", "-q"),
        ("config", "user.name", "t"),
        ("config", "user.email", "t@example.invalid"),
        ("remote", "add", "origin", url),
        ("commit", "-q", "--allow-empty", "-m", "c"),
    ):
        subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True)
    head = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    )
    return head.stdout.strip()


def test_the_pin_refuses_another_repo_another_commit_and_a_changed_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clone = tmp_path / "clone"
    head = _plugin_clone(clone, f"{run.PONYTAIL_REPO}.git")
    monkeypatch.setattr(run, "PONYTAIL_SHA", head)
    run.require_pin(clone)

    (clone / "changed").write_text("x")
    with pytest.raises(SystemExit, match="local changes"):
        run.require_pin(clone)
    (clone / "changed").unlink()

    monkeypatch.setattr(run, "PONYTAIL_SHA", "0" * 40)
    with pytest.raises(SystemExit, match="not at the pin"):
        run.require_pin(clone)

    fake = tmp_path / "fake"
    _plugin_clone(fake, "https://github.com/someone/ponytail-improved")
    with pytest.raises(SystemExit, match="not a clone"):
        run.require_pin(fake)


def test_the_trace_has_one_session_per_arm_on_one_task() -> None:
    plan = plan_sessions(tasks.load_tasks(), 3, "fre1730")
    trace = run.trace_sessions(plan)
    assert [s.arm for s in trace] == ["control", "plugin"]
    assert trace[0].task == trace[1].task
    assert {s.key for s in trace} <= {s.key for s in plan}


def test_a_session_left_out_is_named_and_not_averaged_but_its_spend_counts() -> None:
    good = {
        "exit_code": 0,
        "key": "T-plugin-0",
        "ticket": "T",
        "arm": "plugin",
        "risky": False,
        "subtype": "success",
        "problems": [],
        "is_error": False,
        "cost_usd": 1.0,
        "lines_code": 10,
        "lines_test": 0,
        "pr_tests_fraction": 1.0,
        "tests_added": 0,
        "duration_s": 1,
        "turns": 1,
        "diff_path": "d",
    }
    records = [
        good,
        {**good, "arm": "control", "key": "T-control-0"},
        {
            **good,
            "key": "T-plugin-1",
            "lines_code": 9999,
            "cost_usd": 2.0,
            "is_error": True,
            "subtype": "error",
        },
        {**good, "key": "T-control-1", "arm": "control", "lines_code": 9999, "problems": ["bad"]},
    ]
    text = report.render_report(records, [], "")
    assert "Valid sessions: 2. Sessions left out: 2." in text
    assert "T-plugin-1" in text
    assert "9999" not in text
    assert "$5.00" in text


_REAL_ID = "11111111-2222-3333-4444-555555555555"


@pytest.mark.parametrize(
    "line",
    [
        '{"type":"result","total_cost_usd":-1000000000.0,"is_error":false}',
        '{"type":"result","total_cost_usd":NaN,"is_error":false}',
        '{"type":"result","total_cost_usd":Infinity,"is_error":false}',
        '{"type":"result","total_cost_usd":"1","is_error":false}',
        '{"type":"result","total_cost_usd":true,"is_error":false}',
    ],
)
def test_a_forged_cost_reads_as_no_cost(line: str) -> None:
    assert session._parse_result(line, (), 0, "").cost_usd is None


def test_garbage_on_stdout_does_not_raise() -> None:
    stdout = (
        '[]\n7\nnot json\n{"session_id":"../../*"}\n'
        '{"type":"result","total_cost_usd":1.5,"num_turns":"x","duration_ms":null,"is_error":false,'
        f'"session_id":"{_REAL_ID}"}}\n'
    )
    result = session._parse_result(stdout, (), 0, "")
    assert (result.cost_usd, result.turns, result.duration_s) == (1.5, 0, 0.0)
    assert result.session_id == _REAL_ID


def test_a_forged_session_id_cannot_widen_the_transcript_search(tmp_path: Path) -> None:
    project = tmp_path / "home" / ".claude" / "projects" / "p"
    project.mkdir(parents=True)
    (project / f"{_REAL_ID}.jsonl").write_text("{}")
    assert session.find_transcript(tmp_path / "home", "*") is None
    assert session.find_transcript(tmp_path / "home", _REAL_ID) == project / f"{_REAL_ID}.jsonl"


def test_a_home_swapped_for_a_link_is_not_followed(tmp_path: Path) -> None:
    real = tmp_path / "real"
    project = real / ".claude" / "projects" / "p"
    project.mkdir(parents=True)
    (project / f"{_REAL_ID}.jsonl").write_text("{}")
    link = tmp_path / "home"
    link.symlink_to(real)
    assert session.find_transcript(link, _REAL_ID) is None
    assert snapshot(link) == {".": f"link:{real}"}


def test_a_success_result_from_a_killed_process_is_not_valid_and_stops_the_run() -> None:
    record = {"key": "k", "problems": [], "is_error": False, "exit_code": 137, "subtype": "success"}
    assert not is_valid(record)
    assert "exit code 137" in str(_stop_reason(record))
    assert is_valid({**record, "exit_code": 0})
    assert _stop_reason({**record, "exit_code": 0}) is None


def test_a_directory_at_the_socket_path_does_not_raise_after_the_session(tmp_path: Path) -> None:
    path = tmp_path / "g.sock"
    with serve_gate(path, EgressPolicy()):
        path.unlink()
        path.mkdir()
    assert path.is_dir()


def test_an_added_blank_line_above_an_old_test_is_not_a_new_test() -> None:
    assert not score._ADDED_TEST.findall("+\n def test_existing():\n")
    assert len(score._ADDED_TEST.findall("+def test_new():\n+    async def test_inner():\n")) == 2
    assert not score._ADDED_TEST.findall(" def test_context():\n-def test_gone():\n")


def test_an_edited_or_moved_test_is_not_a_new_test() -> None:
    edited = "-def test_x(a):\n+def test_x(a, b):\n+async def test_new():\n"
    assert score.count_new_tests(edited) == 1
    assert score.count_new_tests("+def test_only():\n") == 1


def test_a_failed_git_call_raises_and_does_not_read_as_a_small_diff(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    failed = subprocess.CompletedProcess([], 128, "", "index.lock exists")
    monkeypatch.setattr(score, "run_in_jail", lambda *_args, **_kwargs: failed)
    with pytest.raises(RunStopped, match="index.lock"):
        score.score_diff(Jail(run_root=tmp_path), "base")


def test_a_plugin_write_into_a_file_that_both_arms_create_is_named() -> None:
    control = [
        {".bashrc": "empty", ".claude.json": "one"},
        {".bashrc": "empty", ".claude.json": "two"},
    ]
    plugin = [{".bashrc": "planted line", ".claude.json": "three"}, {".bashrc": "empty"}]
    assert plugin_changed_content(plugin, control) == (".bashrc",)
    assert plugin_changed_content(control, control) == ()


def test_one_control_session_proves_nothing_about_stable_content() -> None:
    assert plugin_changed_content([{".claude.json": "b"}], [{".claude.json": "a"}]) == ()


def test_the_snapshot_lists_directory_links_empty_and_unreadable_directories(
    tmp_path: Path,
) -> None:
    (tmp_path / "empty").mkdir()
    (tmp_path / "full").mkdir()
    (tmp_path / "full" / "file").write_text("x")
    (tmp_path / "link").symlink_to(tmp_path / "full")
    locked = tmp_path / "locked"
    locked.mkdir()
    (locked / "hidden").write_text("x")
    locked.chmod(0)
    try:
        listing = snapshot(tmp_path)
    finally:
        locked.chmod(0o700)
    assert listing["empty/"] == "empty directory"
    assert listing["link/"] == f"link:{tmp_path / 'full'}"
    assert listing["locked/"].startswith("unreadable:")
    assert "full/file" in listing
    assert "full/" not in listing


def test_paid_has_no_option_that_starts_a_new_ledger(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as stopped:
        run.main(
            ["paid", "--plugin-dir", "x", "--marker", "m", "--hard-stop-usd", "1"]
            + ["--projected-usd", "1", "--owner-go", "go", "--name", "retry"]
        )
    assert stopped.value.code == 2
    assert "unrecognized arguments: --name retry" in capsys.readouterr().err


def test_a_paid_run_needs_a_passed_verification_of_this_task_set(tmp_path: Path) -> None:
    loaded = tasks.load_tasks()
    with pytest.raises(SystemExit, match="verify-tasks"):
        run.require_verified(loaded, tmp_path)

    good = [f"{task.ticket}: ... usable" for task in loaded]
    assert run.record_verified(loaded, good, tmp_path)
    run.require_verified(loaded, tmp_path)
    with pytest.raises(SystemExit, match="verify-tasks"):
        run.require_verified(loaded[:4], tmp_path)
    other_tests = [dataclasses.replace(loaded[0], tests=("tests/other.py",)), *loaded[1:]]
    with pytest.raises(SystemExit, match="verify-tasks"):
        run.require_verified(other_tests, tmp_path)
    assert not run.record_verified(loaded, [*good[:4], "FRE-1: ... NOT USABLE"], tmp_path)


def test_a_verification_that_raises_leaves_no_old_pass_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    loaded = tasks.load_tasks()
    assert run.record_verified(loaded, [f"{t.ticket}: ... usable" for t in loaded], tmp_path)

    def raises(*_args: object) -> list[str]:
        raise RunStopped("uv sync failed")

    monkeypatch.setattr(run, "verify_tasks", raises)
    with pytest.raises(RunStopped):
        run.cmd_verify_tasks(loaded, tmp_path, tmp_path)
    with pytest.raises(SystemExit, match="verify-tasks"):
        run.require_verified(loaded, tmp_path)


def test_a_half_built_environment_is_rebuilt_and_a_failed_build_is_not_marked(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    half = tmp_path / "envs" / "abcdefabcdef" / "bin"
    half.mkdir(parents=True)
    (half / "python").write_text("half")
    monkeypatch.setattr(run, "prepare_repo", lambda _s, _b, target: target.mkdir(parents=True))
    failed = subprocess.CompletedProcess([], 1, "", "no network")
    monkeypatch.setattr(run, "run_in_jail", lambda *_args, **_kwargs: failed)
    with pytest.raises(RunStopped, match="uv sync failed"):
        run.ensure_venv(tmp_path, "abcdefabcdef0000", tmp_path)
    assert not (half / "python").exists()
    assert not (tmp_path / "envs" / "abcdefabcdef.complete").exists()


def test_a_session_at_its_budget_counts_and_an_arm_cannot_hide_its_costly_runs() -> None:
    record = {
        "key": "k",
        "problems": [],
        "is_error": True,
        "exit_code": 1,
        "subtype": "error_max_budget_usd",
        "api_error_status": None,
        "budget_usd": 10.0,
    }
    assert is_valid(record)
    assert _stop_reason(record) is None
    assert not is_valid({**record, "subtype": "error_during_execution"})


def test_a_session_stopped_by_a_budget_the_nearby_cap_shrank_does_not_count() -> None:
    record = {
        "key": "k",
        "problems": [],
        "is_error": True,
        "exit_code": 1,
        "subtype": "error_max_budget_usd",
        "budget_usd": 0.4,
    }
    assert not is_valid(record)


def _left_out_record() -> dict[str, object]:
    return {
        "attempt": "a",
        "key": "T-plugin-0",
        "ticket": "T",
        "arm": "plugin",
        "risky": False,
        "subtype": "error_during_execution",
        "problems": [],
        "is_error": True,
        "exit_code": 1,
        "cost_usd": 10.0,
        "reported_cost_usd": None,
        "refused": ["CONNECT evil.example:443"],
        "footprint": [".config/planted"],
        "ignored_paths": ["hidden.py"],
    }


def test_the_safety_sections_read_a_session_that_was_left_out() -> None:
    text = report.render_report([_left_out_record()], [], "")
    assert "CONNECT evil.example:443" in text
    assert ".config/planted" in text
    assert "hidden.py" in text


def test_the_report_shows_the_charge_and_the_reported_spend_apart() -> None:
    text = report.render_report(
        [
            _left_out_record(),
            {**_left_out_record(), "attempt": "b", "cost_usd": 4.0, "reported_cost_usd": 4.0},
        ],
        [],
        "",
    )
    assert "Charged to the stop, with the sessions left out: $14.00" in text
    assert "Reported by the sessions: $4.00" in text
