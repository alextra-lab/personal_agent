"""FRE-1585 (ADR-0156 D8.2 amendment): reviewed skill scripts run without a card.

An allow rule ``bash(python3 <path> *)`` loads only when ``<path>`` names one file under
``docs/skills/<skill>/scripts/``, exactly. Every other ``python3`` command still asks.

AC-1: the documented example commands of each reviewed script evaluate to allow (the recipes are
read from the skill bodies in ``test_skill_commands_permissions.py``; here the commands are the
plain forms). AC-2: nothing else gains an allow, and the loader refuses every other form. AC-3,
the argument tests of each script, is in ``tests/docs/test_skill_scripts.py``; the last test here
ties the rules to it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from personal_agent.governance.permissions import (
    PermissionRuleError,
    evaluate_permission,
    load_permission_rules,
    parse_rules_document,
)

_REPO = Path(__file__).resolve().parents[3]
RULES = load_permission_rules()

_PROBE = "docs/skills/infrastructure-health/scripts/probe_services.py"
_CYPHER = "docs/skills/neo4j-direct/scripts/cypher.py"
_CLASSIFIER = "docs/skills/query-elasticsearch/scripts/es_index_granularity.py"
_TIMELINE = "docs/skills/turn-forensics/scripts/call_timeline.py"

#: The scripts that a reviewed PR gave an allow rule. A new rule changes this set, and the
#: AC-3 argument test in tests/docs/test_skill_scripts.py must cover the new script.
REVIEWED = {_PROBE, _CYPHER, _CLASSIFIER, _TIMELINE}


def _bash(command: str, mode: str = "NORMAL") -> str:
    return evaluate_permission("bash", {"command": command}, mode, RULES).decision


def _load(rule: str) -> None:
    parse_rules_document({"allow": [{"rule": rule}]}, source="test")


# --- AC-1: the documented commands run without a card --------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        f"python3 {_PROBE} all",
        f"python3 {_PROBE} postgres",
        f'python3 {_CYPHER} "MATCH (e:Entity) RETURN count(e) AS n"',
        f"python3 {_CYPHER} 'MATCH (e:Entity) WHERE toLower(e.name) CONTAINS $q "
        "RETURN e.name AS name LIMIT 10' --param q=memory",
        f'python3 {_CYPHER} --limit 5 "CALL db.labels() YIELD label RETURN label"',
        "curl -fsS 'http://elasticsearch:9200/_cat/indices?h=index' "
        f"| grep '^agent-monitors-joinability-' | python3 {_CLASSIFIER} agent-monitors-joinability",
        f"python3 {_TIMELINE} 0a1b2c3d-trace",
        f"python3 {_PROBE} --help",
    ],
)
@pytest.mark.parametrize("mode", ["NORMAL", "ALERT", "DEGRADED"])
def test_ac1_a_reviewed_script_runs_without_a_card(command: str, mode: str) -> None:
    assert _bash(command, mode) == "allow"


def test_ac1_the_documented_timeline_pipeline_runs_without_a_card() -> None:
    """docker logs has its own rule, in NORMAL only, so the whole pipeline is a NORMAL check."""
    command = (
        f"docker logs cloud-sim-seshat-gateway --since=40m 2>&1 | python3 {_TIMELINE} 0a1b2c3d"
    )
    assert _bash(command) == "allow"


def test_ac1_each_script_has_a_rule_in_the_real_file() -> None:
    named = {
        rule.bash_words[1]
        for rule in RULES.allow
        if rule.bash_words and rule.bash_prefix and rule.bash_words[0] == "python3"
    }
    assert named == REVIEWED


# --- AC-2: nothing else gains an allow ------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "python3 -c 'print(1)'",
        "python3 /tmp/x.py",
        "python3 docs/skills/x/scripts/../../../evil.py",
        "python3 docs/skills/neo4j-direct/scripts/../../infrastructure-health/scripts/"
        "probe_services.py all",
        "python3 docs/skills/*/scripts/cypher.py x",
        "python3 docs/skills/neo4j-direct/scripts/cyph?r.py x",
        "python3 docs/skills/neo4j-direct/scripts/[c]ypher.py x",
        "python3 docs/skills/neo4j-direct/cypher.py x",
        "python3 docs/skills/neo4j-direct/scripts/other.py",
        f"python3 ./{_PROBE} all",
        f"python3 /app/{_PROBE} all",
        f"python3 -I {_PROBE} all",
        f"python3 -m {_PROBE} all",
        f"python {_PROBE} all",
        f"/usr/bin/python3 {_PROBE} all",
        f"python3 {_PROBE}x all",
        f"python3 {_PROBE} all; rm x",
        f"python3 {_PROBE} all && touch x",
        f"python3 {_PROBE} all | sh",
        f"python3 {_PROBE} all & python3 -c x",
        f"python3 {_PROBE} $(id)",
        f"python3 {_PROBE} `id`",
        f'python3 {_PROBE} "$(id)"',
        f"python3 {_PROBE} ${{HOME}}",
        f"python3 {_PROBE} all > /tmp/out",
        f"python3 {_PROBE} all >> /tmp/out",
        f"python3 {_PROBE} all 2> /tmp/err",
        f"python3 {_PROBE} all < /etc/passwd",
        f"python3 {_PROBE} all <<< x",
        f"PATH=/tmp python3 {_PROBE} all",
        f"PYTHONPATH=/tmp python3 {_PROBE} all",
        f"python3 {_PROBE} *",
        f"python3 {_PROBE} ~root",
        f"(python3 {_PROBE} all)",
        f"timeout 5 python3 {_PROBE} all",
        f"env python3 {_PROBE} all",
        f"cd /tmp && python3 {_PROBE} all",
        f"python3 {_PROBE} all <(id)",
        f"python3 {_PROBE} all\nchmod 777 x",
    ],
)
def test_ac2_every_other_python3_command_asks(command: str) -> None:
    assert _bash(command) in ("ask", "deny"), command


def test_ac2_a_separator_does_not_widen_what_the_next_command_may_do() -> None:
    """D8.2 judges each subcommand alone: the script rule allows the script, nothing after it."""
    assert _bash(f"python3 {_PROBE} all && ls /tmp") == "allow"  # ls has its own rule
    assert _bash(f"python3 {_PROBE} all && touch x") == "ask"


@pytest.mark.parametrize(
    "rule",
    [
        "bash(python3 /tmp/x.py *)",
        "bash(python3 docs/skills/x/y.py *)",
        "bash(python3 docs/skills/neo4j-direct/cypher.py *)",
        "bash(python3 docs/skills/x/scripts/../../../evil.py *)",
        "bash(python3 docs/skills/neo4j-direct/scripts/../scripts/cypher.py *)",
        "bash(python3 docs/skills/*/scripts/cypher.py *)",
        "bash(python3 docs/skills/neo4j-direct/scripts/*.py *)",
        "bash(python3 docs/skills/neo4j-direct/scripts/cyph?r.py *)",
        f"bash(python3 ./{_CYPHER} *)",
        f"bash(python3 /app/{_CYPHER} *)",
        "bash(python3 docs/skills/neo4j-direct/scripts/missing.py *)",
        "bash(python3 docs/skills/neo4j-direct/scripts/__pycache__ *)",
        f"bash(python3 {_PROBE} all *)",
        f"bash(python3 -I {_PROBE} *)",
        "bash(python3 *)",
        "bash(python *)",
        f"bash(python {_PROBE} *)",
    ],
)
def test_ac2_the_loader_refuses_any_other_python3_prefix_rule(rule: str) -> None:
    with pytest.raises(PermissionRuleError):
        _load(rule)


def test_ac2_the_loader_refuses_a_symlinked_script(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scripts = tmp_path / "docs" / "skills" / "evil" / "scripts"
    scripts.mkdir(parents=True)
    target = tmp_path / "payload.py"
    target.write_text("print('x')\n")
    (scripts / "link.py").symlink_to(target)
    (scripts / "real.py").write_text("print('x')\n")
    monkeypatch.chdir(tmp_path)
    _load("bash(python3 docs/skills/evil/scripts/real.py *)")  # the control: a real file loads
    with pytest.raises(PermissionRuleError):
        _load("bash(python3 docs/skills/evil/scripts/link.py *)")


def test_ac2_the_loader_refuses_a_script_under_a_symlinked_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "a.py").write_text("print('x')\n")
    skill = tmp_path / "docs" / "skills" / "evil"
    skill.mkdir(parents=True)
    (skill / "scripts").symlink_to(elsewhere, target_is_directory=True)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(PermissionRuleError):
        _load("bash(python3 docs/skills/evil/scripts/a.py *)")


def test_ac2_the_path_is_read_from_the_working_directory_of_bash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bash tool inherits the gateway's directory, so the loader checks the file there."""
    monkeypatch.chdir(tmp_path)
    with pytest.raises(PermissionRuleError):
        _load(f"bash(python3 {_CYPHER} *)")
    monkeypatch.chdir(_REPO)
    _load(f"bash(python3 {_CYPHER} *)")


def test_ac2_an_exact_python3_rule_stays_as_d82_had_it() -> None:
    """D8.2: an exact rule may name the full command of a program-running word."""
    _load("bash(python3 /tmp/x.py)")


# --- the file the rule trusts cannot be changed without a card ------------------------------


@pytest.mark.parametrize("tool", ["write", "edit"])
@pytest.mark.parametrize(
    "path",
    [
        "/app/docs/skills/infrastructure-health/scripts/probe_services.py",
        "/app/docs/skills/infrastructure-health/scripts/json.py",
        "/app/.venv/lib/python3.12/site-packages/evil.pth",
    ],
)
def test_write_and_edit_refuse_the_script_and_its_import_path(tool: str, path: str) -> None:
    from personal_agent.tools.primitives._governance import (
        PathRefusalError,
        _check_path_governance,
    )

    with pytest.raises(PathRefusalError) as exc_info:
        _check_path_governance(Path(path), tool)
    assert exc_info.value.code == "forbidden_path"
