"""FRE-1730: AC-2, AC-3 and AC-4 against the real ``claude`` binary, in the real jail, with a stub model.

CI has no ``claude`` and no ``bwrap``, so these tests skip there. The evidence for them is the local run in the PR.
"""

from __future__ import annotations

import json
import shutil
import socket
import subprocess
from collections.abc import Iterator, Mapping
from pathlib import Path

import pytest
from scripts.eval.fre1730.jail import Jail, main_repo_root, run_in_jail
from scripts.eval.fre1730.matrix import PlannedSession, RunConfig, plan_sessions, run_matrix
from scripts.eval.fre1730.run import FIXTURE_MARKER, FIXTURE_PLUGIN, MODEL
from scripts.eval.fre1730.score import copy_pr_tests, ignored_paths, score_diff
from scripts.eval.fre1730.session import (
    find_transcript,
    paid_route,
    prepare_home,
    prepare_repo,
    run_session,
    stub_route,
)
from scripts.eval.fre1730.spend import RunStopped, SpendStop
from scripts.eval.fre1730.stub_model import StubModel, serve
from scripts.eval.fre1730.tasks import Task
from scripts.eval.fre1730.transcript import arm_problems, read_transcript

pytestmark = pytest.mark.skipif(
    not all(shutil.which(tool) for tool in ("claude", "bwrap", "socat", "systemd-run")),
    reason="needs the claude, bwrap, socat and systemd-run binaries",
)
_NO_RETRY = {"CLAUDE_CODE_MAX_RETRIES": "0"}
_GLOBAL_RULES = "GLOBAL RULES FOR THE TEST\n"
_PROJECT_RULES = "PROJECT RULES FOR THE TEST\n"
#: Opus 5.5 at $4 per MTok of input: one reply of 500,000 input tokens costs $2.
_TWO_DOLLAR_INPUT_TOKENS = 500_000


@pytest.fixture
def source(tmp_path: Path) -> Path:
    """Make a small repo with a CLAUDE.md, and a commit on top of its base."""
    repo = tmp_path / "source"
    repo.mkdir()
    (repo / "CLAUDE.md").write_text(_PROJECT_RULES)
    settings = repo / ".claude"
    settings.mkdir()
    (settings / "settings.json").write_text(json.dumps({"permissions": {"allow": ["Bash"]}}))
    for args in (
        ("init", "-q"),
        ("config", "user.name", "t"),
        ("config", "user.email", "t@example.invalid"),
        ("add", "-A"),
        ("commit", "-qm", "base"),
        ("commit", "-q", "--allow-empty", "-m", "the merged fix"),
    ):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
    return repo


@pytest.fixture
def stub() -> Iterator[tuple[StubModel, tuple[str, int]]]:
    """Serve a stub."""
    model = StubModel()
    with serve(model) as url:
        yield model, ("127.0.0.1", int(url.rsplit(":", 1)[1]))


def _head(source: Path, ref: str = "HEAD") -> str:
    done = subprocess.run(
        ["git", "-C", str(source), "rev-parse", ref], capture_output=True, text=True, check=True
    )
    return done.stdout.strip()


def _session(
    tmp_path: Path,
    source: Path,
    address: tuple[str, int],
    *,
    plugin: Path | None,
    global_rules: bool = True,
    budget_usd: float = 5.0,
    env: Mapping[str, str] | None = None,
) -> tuple[Path, str]:
    run_root = tmp_path / "run"
    shutil.rmtree(run_root, ignore_errors=True)
    run_root.mkdir()
    rules = tmp_path / "global.md"
    rules.write_text(_GLOBAL_RULES)
    prepare_repo(source, _head(source), run_root / "repo")
    prepare_home(run_root / "home", rules if global_rules else None)
    result = run_session(
        run_root=run_root,
        prompt="say hi",
        model=MODEL,
        route=stub_route(),
        budget_usd=budget_usd,
        timeout_s=120,
        plugin=plugin,
        stub_address=address,
        extra_env={**_NO_RETRY, **(env or {})},
    )
    assert result.session_id, result.stderr_tail
    transcript = find_transcript(run_root / "home", result.session_id)
    assert transcript is not None
    return transcript, result.session_id


def test_ac2_the_plugin_arm_gets_the_marker_and_both_arms_get_claude_md(
    tmp_path: Path, source: Path, stub: tuple[StubModel, tuple[str, int]]
) -> None:
    model, address = stub
    transcript, _ = _session(tmp_path, source, address, plugin=FIXTURE_PLUGIN)
    facts = read_transcript(transcript, FIXTURE_MARKER)
    assert facts.marker_sent
    assert facts.instructions_sent == {"User", "Project"}
    assert arm_problems(facts, "plugin") == []
    assert FIXTURE_MARKER in json.dumps(model.requests[-1])
    assert _GLOBAL_RULES.strip() in json.dumps(model.requests[-1])

    transcript, _ = _session(tmp_path, source, address, plugin=None)
    facts = read_transcript(transcript, FIXTURE_MARKER)
    assert not facts.marker_sent
    assert facts.instructions_sent == {"User", "Project"}
    assert arm_problems(facts, "control") == []
    assert FIXTURE_MARKER not in json.dumps(model.requests[-1])


def test_ac2_seeded_negatives_on_real_transcripts(
    tmp_path: Path, source: Path, stub: tuple[StubModel, tuple[str, int]]
) -> None:
    _, address = stub
    with_plugin, _ = _session(tmp_path, source, address, plugin=FIXTURE_PLUGIN)
    facts = read_transcript(with_plugin, FIXTURE_MARKER)
    assert "control arm: the marker reached the model" in arm_problems(facts, "control")

    empty_plugin = tmp_path / "empty_plugin"
    (empty_plugin / ".claude-plugin").mkdir(parents=True)
    (empty_plugin / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "empty", "version": "0.0.1"})
    )
    no_marker, _ = _session(tmp_path, source, address, plugin=empty_plugin)
    facts = read_transcript(no_marker, FIXTURE_MARKER)
    assert "plugin arm: the marker did not reach the model" in arm_problems(facts, "plugin")

    no_global, _ = _session(tmp_path, source, address, plugin=None, global_rules=False)
    facts = read_transcript(no_global, FIXTURE_MARKER)
    assert "control arm: the User CLAUDE.md did not reach the model" in arm_problems(
        facts, "control"
    )


def _config(tmp_path: Path, source: Path, address: tuple[str, int]) -> RunConfig:
    rules = tmp_path / "global.md"
    rules.write_text(_GLOBAL_RULES)
    return RunConfig(
        source=source,
        scratch=tmp_path / "scratch",
        results=tmp_path / "results",
        model=MODEL,
        route=stub_route(),
        plugin=FIXTURE_PLUGIN,
        marker=FIXTURE_MARKER,
        global_claude_md=rules,
        venvs={},
        stub_address=address,
        extra_env=_NO_RETRY,
    )


def _task(source: Path) -> Task:
    return Task("FRE-1721", 1, _head(source), (), True, "test")


def test_ac3_the_stop_ends_the_run_and_no_more_sessions_start(
    tmp_path: Path, source: Path, stub: tuple[StubModel, tuple[str, int]]
) -> None:
    model, address = stub
    model.input_tokens = _TWO_DOLLAR_INPUT_TOKENS
    sessions = plan_sessions([_task(source)], 3, "seed")
    config = _config(tmp_path, source, address)
    stop = SpendStop(hard_stop_usd=3.0, projected_usd=100.0)
    with pytest.raises(RunStopped, match="reached the cap"):
        run_matrix(sessions, config, stop)
    assert len(model.requests) == 2
    assert stop.spent_usd >= 3.0
    assert len(config.ledger.read_text().splitlines()) == 2


def test_ac3_a_restart_keeps_the_spend_and_cannot_reset_the_stop(
    tmp_path: Path, source: Path, stub: tuple[StubModel, tuple[str, int]]
) -> None:
    model, address = stub
    model.input_tokens = _TWO_DOLLAR_INPUT_TOKENS
    sessions = plan_sessions([_task(source)], 3, "seed")
    config = _config(tmp_path, source, address)
    with pytest.raises(RunStopped):
        run_matrix(sessions, config, SpendStop(hard_stop_usd=3.0, projected_usd=100.0))
    before = len(model.requests)
    with pytest.raises(RunStopped, match="reached the cap"):
        run_matrix(sessions, config, SpendStop(hard_stop_usd=3.0, projected_usd=100.0))
    assert len(model.requests) == before


def test_ac3_the_budget_stops_a_session_inside_its_tool_loop(
    tmp_path: Path, source: Path, stub: tuple[StubModel, tuple[str, int]]
) -> None:
    model, address = stub
    model.input_tokens = _TWO_DOLLAR_INPUT_TOKENS
    model.tool_loop = True
    sessions = [PlannedSession(_task(source), "control", 0)]
    config = _config(tmp_path, source, address)
    stop = SpendStop(hard_stop_usd=3.0, projected_usd=100.0)
    records = run_matrix(sessions, config, stop)
    assert records[0]["subtype"] == "error_max_budget_usd"
    assert len(model.requests) == 2
    with pytest.raises(RunStopped, match="reached the cap"):
        stop.check()


def test_a_quota_error_stops_the_run(
    tmp_path: Path, source: Path, stub: tuple[StubModel, tuple[str, int]]
) -> None:
    model, address = stub
    model.status = 429
    sessions = plan_sessions([_task(source)], 3, "seed")
    with pytest.raises(RunStopped, match="API status 429"):
        run_matrix(sessions, _config(tmp_path, source, address), SpendStop(3.0, 3.0))
    assert len(model.requests) == 1


def test_a_restart_runs_an_errored_session_again_and_keeps_its_spend(
    tmp_path: Path, source: Path, stub: tuple[StubModel, tuple[str, int]]
) -> None:
    model, address = stub
    model.status = 429
    sessions = [PlannedSession(_task(source), "control", 0)]
    config = _config(tmp_path, source, address)
    with pytest.raises(RunStopped):
        run_matrix(sessions, config, SpendStop(3.0, 3.0))
    model.status = 200
    records = run_matrix(sessions, config, SpendStop(3.0, 3.0))
    assert [r["is_error"] for r in records] == [True, False]
    assert len(config.ledger.read_text().splitlines()) == 2


def test_ac4_the_listing_names_each_file_that_only_the_plugin_wrote(
    tmp_path: Path, source: Path, stub: tuple[StubModel, tuple[str, int]]
) -> None:
    _, address = stub
    task = _task(source)
    config = _config(tmp_path, source, address)
    records = run_matrix(
        [PlannedSession(task, "plugin", 0), PlannedSession(task, "control", 0)],
        config,
        SpendStop(5.0, 5.0),
    )
    plugin, control = records
    assert "added    .claude/.fre1730-fixture-active" in str(plugin["footprint_lines"])
    assert "added    .config/fre1730-fixture/ledger.csv" in str(plugin["footprint_lines"])
    assert "fre1730-fixture" not in str(control["footprint_lines"])
    assert ".claude/projects/" in str(control["footprint_lines"])


def test_the_real_home_does_not_change_when_a_plugin_writes_to_its_home(
    tmp_path: Path, source: Path, stub: tuple[StubModel, tuple[str, int]]
) -> None:
    _, address = stub
    assert not (Path.home() / ".claude" / ".fre1730-fixture-active").exists()
    _session(tmp_path, source, address, plugin=FIXTURE_PLUGIN)
    assert not (Path.home() / ".claude" / ".fre1730-fixture-active").exists()
    assert not (Path.home() / ".config" / "fre1730-fixture").exists()


def test_bash_is_on_and_a_call_sees_no_credential_and_no_network(
    tmp_path: Path, source: Path, stub: tuple[StubModel, tuple[str, int]]
) -> None:
    model, address = stub
    model.input_tokens = _TWO_DOLLAR_INPUT_TOKENS
    model.tool_loop = True
    model.tool_command = (
        "env | cut -d= -f1 > bash_env.txt; "
        "curl -s -m 3 https://example.com -o /dev/null; echo curl=$? >> bash_env.txt"
    )
    _session(
        tmp_path,
        source,
        address,
        plugin=None,
        budget_usd=3.0,
        env={"CLAUDE_CODE_OAUTH_TOKEN": "dummy-for-the-test"},
    )
    names = (tmp_path / "run" / "repo" / "bash_env.txt").read_text().split()
    assert "HOME" in names
    assert "ANTHROPIC_API_KEY" not in names
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in names
    assert "curl=0" not in names


def test_the_jail_hides_the_tree_the_home_and_loopback(tmp_path: Path) -> None:
    run_root = tmp_path / "run"
    (run_root / "repo").mkdir(parents=True)
    listener = socket.create_server(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    probe = (
        f"import os, socket\n"
        f"print('repo', len(os.listdir({str(main_repo_root())!r})))\n"
        f"print('env', os.path.exists({str(main_repo_root() / '.env')!r}))\n"
        f"print('claude', os.path.exists({str(Path.home() / '.claude')!r}))\n"
        f"print('docker', os.path.exists('/var/run/docker.sock'))\n"
        f"s = socket.socket()\n"
        f"s.settimeout(2)\n"
        f"print('loopback', s.connect_ex(('127.0.0.1', {port})) == 0)\n"
    )
    try:
        done = run_in_jail(Jail(run_root=run_root), ["/usr/bin/python3", "-c", probe])
    finally:
        listener.close()
    assert done.returncode == 0, done.stderr
    assert done.stdout.split("\n")[:5] == [
        "repo 0",
        "env False",
        "claude False",
        "docker False",
        "loopback False",
    ]


def test_the_scope_kills_a_process_over_its_memory_limit(tmp_path: Path) -> None:
    run_root = tmp_path / "run"
    (run_root / "repo").mkdir(parents=True)
    jail = Jail(run_root=run_root, memory_max="200M")
    code = "x = bytearray(400 * 1024 * 1024); x[-1] = 1; print('NOT LIMITED')"
    done = run_in_jail(jail, ["/usr/bin/python3", "-c", code])
    assert "NOT LIMITED" not in done.stdout
    assert done.returncode != 0
    small = run_in_jail(jail, ["/usr/bin/python3", "-c", "print('small ok')"])
    assert small.stdout.strip() == "small ok"


def test_the_paid_route_goes_only_through_the_gate(tmp_path: Path, source: Path) -> None:
    """The gate refuses the host here, so no request reaches a model and no token is needed."""
    run_root = tmp_path / "run"
    run_root.mkdir()
    prepare_repo(source, _head(source), run_root / "repo")
    prepare_home(run_root / "home", None)
    result = run_session(
        run_root=run_root,
        prompt="say hi",
        model=MODEL,
        route=paid_route({"FRE1730_CLAUDE_OAUTH_TOKEN": "dummy-for-the-test"}),
        budget_usd=1.0,
        timeout_s=120,
        extra_env=_NO_RETRY,
        allowed_hosts=frozenset(),
    )
    assert "CONNECT api.anthropic.com:443" in result.refused
    assert result.is_error


def test_a_session_cannot_see_the_merged_fix(tmp_path: Path, source: Path) -> None:
    run_root = tmp_path / "run"
    run_root.mkdir()
    base = _head(source, "HEAD~1")
    prepare_repo(source, base, run_root / "repo")
    done = run_in_jail(Jail(run_root=run_root), ["git", "log", "--all", "--format=%s"])
    assert done.stdout.split() == ["base"]
    assert run_in_jail(Jail(run_root=run_root), ["git", "remote"]).stdout == ""


def test_the_score_reads_a_real_change_committed_or_not(tmp_path: Path, source: Path) -> None:
    run_root = tmp_path / "run"
    run_root.mkdir()
    base = _head(source)
    repo = run_root / "repo"
    prepare_repo(source, base, repo)
    (repo / "app.py").write_text("a = 1\nb = 2\n")
    subprocess.run(["git", "-C", str(repo), "add", "app.py"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "work"], check=True)
    (repo / "tests").mkdir()
    (repo / "tests" / "test_app.py").write_text(
        "def test_one():\n    pass\n\n\nasync def test_two():\n    pass\n\n\ndef helper():\n    pass\n"
    )
    (repo / "app.py").write_text("a = 1\nb = 3\n")
    score = score_diff(Jail(run_root=run_root), base)
    assert (score.lines_code, score.tests_added) == (2, 2)
    assert score.lines_test == 10
    assert "+b = 3" in score.diff


def test_a_link_in_the_session_repo_cannot_write_to_the_host(tmp_path: Path, source: Path) -> None:
    host_file = tmp_path / "host_file.txt"
    host_file.write_text("host content")
    test_file = "tests/test_merged.py"
    (source / "tests").mkdir()
    (source / test_file).write_text("MERGED\n")
    subprocess.run(["git", "-C", str(source), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(source), "commit", "-qm", "merged"], check=True)
    run_root = tmp_path / "run"
    run_root.mkdir()
    repo = run_root / "repo"
    prepare_repo(source, _head(source, "HEAD~1"), repo)
    (repo / "tests").mkdir()
    (repo / test_file).symlink_to(host_file)
    task = Task("FRE-1721", 1, _head(source), (test_file,), True, "test")

    copy_pr_tests(source, task, Jail(run_root=run_root))

    assert host_file.read_text() == "host content"
    assert not (repo / test_file).is_symlink()
    assert (repo / test_file).read_text() == "MERGED\n"


def test_a_file_hidden_by_an_ignore_rule_is_listed(tmp_path: Path, source: Path) -> None:
    run_root = tmp_path / "run"
    run_root.mkdir()
    repo = run_root / "repo"
    prepare_repo(source, _head(source), repo)
    (repo / ".gitignore").write_text("solution.py\n__pycache__\n")
    (repo / "solution.py").write_text("x = 1\n")
    (repo / "__pycache__").mkdir()
    (repo / "__pycache__" / "a.pyc").write_text("x")
    jail = Jail(run_root=run_root)
    assert (
        "solution.py"
        not in score_diff(jail, _head(source)).diff.replace(".gitignore", "").split("+++")[0]
    )
    assert ignored_paths(jail) == ["solution.py"]
