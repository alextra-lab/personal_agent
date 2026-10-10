"""ADR-0156 D8.1: the rule grammar, the loader, the order, and failing closed (FRE-1574)."""

from __future__ import annotations

from pathlib import Path

import pytest

from personal_agent.governance import permissions
from personal_agent.governance.permissions import (
    PermissionRuleError,
    PermissionRules,
    decide_permission,
    evaluate_permission,
    load_permission_rules,
    parse_rule,
    parse_rules_document,
)


def _rules(**lists: list[str]) -> PermissionRules:
    return PermissionRules(
        **{
            key: tuple(parse_rule(text, reason="r") for text in value)
            for key, value in lists.items()
        }
    )


# --- grammar: one test per form ----------------------------------------------------------


def test_bare_tool_matches_every_call() -> None:
    rules = _rules(allow=["web_search"])
    assert evaluate_permission("web_search", {"query": "x"}, "NORMAL", rules).decision == "allow"
    assert evaluate_permission("web_search", {}, "NORMAL", rules).decision == "allow"


def test_bash_exact_form() -> None:
    rules = _rules(allow=["bash(git status)"])
    assert (
        evaluate_permission("bash", {"command": "git status"}, "NORMAL", rules).decision == "allow"
    )
    assert (
        evaluate_permission("bash", {"command": "git status -s"}, "NORMAL", rules).decision == "ask"
    )


def test_bash_prefix_form_includes_the_words_alone() -> None:
    rules = _rules(allow=["bash(git log *)"])
    for command in ("git log", "git log --oneline -3"):
        assert (
            evaluate_permission("bash", {"command": command}, "NORMAL", rules).decision == "allow"
        )
    assert evaluate_permission("bash", {"command": "git push"}, "NORMAL", rules).decision == "ask"


def test_typed_parameter_form() -> None:
    rules = _rules(ask=["run_python(network=true)"], allow=["run_python"])
    assert (
        evaluate_permission("run_python", {"code": "1", "network": True}, "NORMAL", rules).decision
        == "ask"
    )
    for args in ({"code": "1"}, {"code": "1", "network": False}, {"code": "1", "network": 1}):
        assert evaluate_permission("run_python", args, "NORMAL", rules).decision == "allow"


def test_bare_string_and_prefix_forms() -> None:
    rules = _rules(allow=["fetch_url(url=https://docs.python.org/*)", "web_search(categories=it)"])
    ok = evaluate_permission("fetch_url", {"url": "https://docs.python.org/3/"}, "NORMAL", rules)
    other = evaluate_permission("fetch_url", {"url": "https://evil.example/"}, "NORMAL", rules)
    exact = evaluate_permission("web_search", {"categories": "it"}, "NORMAL", rules)
    not_exact = evaluate_permission("web_search", {"categories": "itx"}, "NORMAL", rules)
    assert (ok.decision, other.decision, exact.decision, not_exact.decision) == (
        "allow",
        "ask",
        "allow",
        "ask",
    )


def test_quoted_string_forms() -> None:
    rule = parse_rule('notes_search(query="a, b)")')
    assert rule.matches_call({"query": "a, b)"})
    assert not rule.matches_call({"query": "a, b"})
    prefix = parse_rule('fetch_url(url="https://a.example/x,y"*)')
    assert prefix.matches_call({"url": "https://a.example/x,y/z"})


def test_number_and_null_forms() -> None:
    rule = parse_rule("recall_personal_history(limit=5, days_ago=null)")
    assert rule.matches_call({"limit": 5, "days_ago": None})
    assert not rule.matches_call({"limit": "5", "days_ago": None})


def test_unnamed_parameters_are_free_and_absent_ones_do_not_match() -> None:
    rule = parse_rule("run_python(network=true)")
    assert rule.matches_call({"network": True, "code": "anything"})
    assert not rule.matches_call({"code": "anything"})


def test_session_form_matches_only_the_full_argument_set() -> None:
    rule = parse_rule('bash(={"command": "rm x"})', allow_session_form=True)
    assert rule.matches_call({"command": "rm x"})
    assert not rule.matches_call({"command": "rm x", "timeout_seconds": 5})


# --- loader --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("document", "message"),
    [
        ({"allow": [{"rule": "bash(python3 *)"}]}, "can run another program"),
        ({"allow": [{"rule": "bash(sed *)"}]}, "can run another program"),
        ({"allow": [{"rule": "bash"}]}, "every bash command"),
        ({"allow": [{"rule": "bash(*)"}]}, "no words"),
        ({"allow": [{"rule": "bash(ls * x)"}]}, "only as the last word"),
        ({"allow": [{"rule": "web_search()"}]}, "empty parentheses"),
        ({"allow": [{"rule": 'bash(={"command": "ls"})'}]}, "session form"),
        ({"deny": [{"rule": "bash(rm *)"}]}, "needs a reason"),
        ({"allow": [{"rule": "read", "modes": ["SOMETIMES"]}]}, "modes must be"),
        ({"allow": [{"rule": "read", "when": "x"}]}, "unknown keys"),
        ({"permit": []}, "unknown keys"),
        ({"allow": [{"rule": "read(path=a, path=b)"}]}, "named twice"),
        ({"allow": [{"rule": "read(path)"}]}, "expected name=value"),
    ],
)
def test_loader_rejects(document: dict[str, object], message: str) -> None:
    with pytest.raises(PermissionRuleError, match=message):
        parse_rules_document(document, source="test")


def test_exact_rule_may_name_an_indirect_word() -> None:
    rules = parse_rules_document({"allow": [{"rule": "bash(timeout 30 git status)"}]}, source="t")
    assert (
        evaluate_permission("bash", {"command": "timeout 30 git status"}, "NORMAL", rules).decision
        == "allow"
    )
    assert (
        evaluate_permission(
            "bash", {"command": "timeout 30 git status -s"}, "NORMAL", rules
        ).decision
        == "ask"
    )


def test_load_merges_files_in_order(tmp_path: Path) -> None:
    base = tmp_path / "permissions.yaml"
    extra = tmp_path / "extra.yaml"
    base.write_text("allow:\n  - rule: read\n")
    extra.write_text("allow:\n  - rule: write\n")
    rules = load_permission_rules([base, extra])
    assert [r.text for r in rules.allow] == ["read", "write"]


# --- order and modes -----------------------------------------------------------------------


def test_deny_beats_ask_beats_allow() -> None:
    rules = _rules(deny=["bash(rm *)"], ask=["bash(rm *)", "bash(git *)"], allow=["bash(git *)"])
    assert evaluate_permission("bash", {"command": "rm x"}, "NORMAL", rules).decision == "deny"
    decision = evaluate_permission("bash", {"command": "git status"}, "NORMAL", rules)
    assert (decision.decision, decision.explicit) == ("ask", True)


def test_deny_applies_inside_nested_commands() -> None:
    rules = _rules(deny=["bash(rm *)"], allow=["bash(ls *)"])
    decision = evaluate_permission("bash", {"command": "ls $(rm -f x)"}, "NORMAL", rules)
    assert (decision.decision, decision.refused) == ("deny", "rm -f x")


def test_rules_scoped_to_a_mode() -> None:
    rules = PermissionRules(
        ask=(parse_rule("write", modes=["ALERT"]),), allow=(parse_rule("write"),)
    )
    assert evaluate_permission("write", {}, "NORMAL", rules).decision == "allow"
    assert evaluate_permission("write", {}, "ALERT", rules).decision == "ask"


def test_unmatched_call_asks_without_explicit_flag() -> None:
    decision = evaluate_permission(
        "create_linear_issue", {"title": "x"}, "NORMAL", PermissionRules()
    )
    assert (decision.decision, decision.explicit, decision.rule) == ("ask", False, None)


def test_refused_subcommand_is_named() -> None:
    rules = _rules(allow=["bash(ls *)"])
    decision = evaluate_permission("bash", {"command": "ls && rm x"}, "NORMAL", rules)
    assert decision.refused == "rm x"


# --- fail closed (D8.1) ---------------------------------------------------------------------


def test_missing_rules_file_denies(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(permissions, "rules_file_paths", lambda: [tmp_path / "missing.yaml"])
    decision = decide_permission("bash", {"command": "ls"}, "NORMAL")
    assert decision.decision == "deny"
    assert decision.refused == "ls"
    assert "could not be evaluated" in decision.reason


def test_broken_rules_file_denies(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    broken = tmp_path / "permissions.yaml"
    broken.write_text("allow:\n  - rule: bash(env *)\n")
    monkeypatch.setattr(permissions, "rules_file_paths", lambda: [broken])
    assert decide_permission("bash", {"command": "env"}, "NORMAL").decision == "deny"


def test_evaluation_error_denies(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(permissions, "evaluate_permission", boom)
    decision = decide_permission("read", {"path": "/x"}, "NORMAL", rules=PermissionRules())
    assert decision.decision == "deny"
