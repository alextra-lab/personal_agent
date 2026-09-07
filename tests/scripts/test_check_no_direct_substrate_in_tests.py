# ruff: noqa: D103
"""Unit tests for the substrate-access guard (FRE-375), including its FRE-1372 AC-3
extension: an eval script driving a turn against the isolated eval gateway
(``EVAL_ARMS``/``EVAL_CHAT_BASE_URL``) must also reference ``IsolatedArmRunner``.

Drives `find_violations` directly (synthetic path->content dicts, no git, no real repo
tree) — same shape as `tests/scripts/test_check_no_deployment_identifier.py`.

The seeded negative (`test_hand_rolled_eval_turn_is_flagged`) is required, not
decorative: a guard whose test set never contains an actual bad file proves nothing —
see this module's own header for the master review that found the pre-FRE-1372
`IsolatedArmRunner` shipped as an unenforced, opt-in helper.
"""

from __future__ import annotations

from scripts.check_no_direct_substrate_in_tests import _is_target_file, find_violations


def _scan(files: dict[str, str]) -> list[str]:
    return find_violations(files.keys(), files.__getitem__)


# ── Pre-existing substrate-access patterns (unchanged behavior) ────────────────────


def test_clean_file_has_no_violations() -> None:
    files = {"tests/test_example.py": "def test_ok() -> None:\n    assert True\n"}
    assert _scan(files) == []


def test_bare_memory_service_is_flagged() -> None:
    files = {"tests/test_bad.py": "svc = MemoryService()\n"}  # fre-375-allow: seeded fixture data
    violations = _scan(files)
    assert len(violations) == 1
    assert "tests/test_bad.py:1" in violations[0]


def test_exempted_line_is_not_flagged() -> None:
    files = {"tests/test_ok.py": "svc = MemoryService()  # fre-375-allow: explicit fixture setup\n"}
    assert _scan(files) == []


def test_file_outside_scan_prefixes_is_not_a_target() -> None:
    """`find_violations` scans whatever paths it is given — `main()` is what filters to
    `_SCAN_PREFIXES` via `_is_target_file` before calling it, so that filtering is
    tested at its own boundary rather than through `find_violations`.
    """
    assert _is_target_file("src/personal_agent/memory/service.py") is False


# ── FRE-1372 AC-3: eval-turn isolation ──────────────────────────────────────────────


def test_hand_rolled_eval_turn_is_flagged() -> None:
    """The seeded negative: a fixture script that references the eval gateway's guard
    constants and posts a turn directly, with no reference to `IsolatedArmRunner`
    anywhere, must be caught — this is exactly the failure AC-3 rules out ("isolation
    depends on each script remembering to call a reset helper"), reproduced on purpose.
    """
    files = {
        "scripts/eval/fre9999_hand_rolled/harness.py": (
            "from scripts.eval.fre1337_intent_probe.substrate import EVAL_ARMS\n\n"
            "async def drive_turn(client, message):\n"
            "    return await client.post(\n"
            '        f"{EVAL_ARMS[\'control\']}/chat", params={"message": message}\n'
            "    )\n"
        )
    }
    violations = _scan(files)
    assert len(violations) == 1
    assert "scripts/eval/fre9999_hand_rolled/harness.py" in violations[0]
    assert "IsolatedArmRunner" in violations[0]


def test_eval_chat_base_url_marker_alone_is_also_flagged() -> None:
    files = {
        "scripts/eval/fre9999_hand_rolled/other.py": (
            "from scripts.eval.fre1337_intent_probe.substrate import EVAL_CHAT_BASE_URL\n\n"
            "URL = EVAL_CHAT_BASE_URL\n"
        )
    }
    assert len(_scan(files)) == 1


def test_script_referencing_isolated_arm_runner_is_not_flagged() -> None:
    """The intended, structural path: using `IsolatedArmRunner` to drive the turn."""
    files = {
        "scripts/eval/fre9999_ok/harness.py": (
            "from scripts.eval.eval_isolation import IsolatedArmRunner\n"
            "from scripts.eval.fre1337_intent_probe.substrate import EVAL_ARMS\n\n"
            "async def drive_turn(runner, http, es, message):\n"
            '    return await runner.run_turn(http, es, message, arm="control")\n'
        )
    }
    assert _scan(files) == []


def test_eval_isolation_module_itself_is_exempt() -> None:
    """`eval_isolation.py` IS `IsolatedArmRunner` — it cannot reference its own name as
    an import, so it is exempted by path rather than by content.
    """
    files = {
        "scripts/eval/eval_isolation.py": (
            "from scripts.eval.fre1337_intent_probe.substrate import EVAL_ARMS\n\n"
            "class IsolatedArmRunner:\n"
            "    pass\n"
        )
    }
    assert _scan(files) == []


def test_substrate_module_itself_is_exempt() -> None:
    """`substrate.py` defines `EVAL_ARMS`/`EVAL_CHAT_BASE_URL` — it references them by
    definition, never to drive a turn, so it is exempted by path.
    """
    files = {
        "scripts/eval/fre1337_intent_probe/substrate.py": (
            'EVAL_ARMS = {"control": "http://localhost:9002"}\n'
            'EVAL_CHAT_BASE_URL = "http://localhost:9002"\n'
        )
    }
    assert _scan(files) == []


def test_file_outside_scripts_eval_referencing_eval_arms_is_not_checked() -> None:
    """The eval-turn-isolation check is scoped to `scripts/eval/` — the only tree that
    ever drives a turn against the isolated eval gateway.
    """
    files = {
        "tests/evaluation/test_something.py": (
            "from scripts.eval.fre1337_intent_probe.substrate import EVAL_ARMS\n\n"
            "URL = EVAL_ARMS['control']\n"
        )
    }
    assert _scan(files) == []


def test_file_with_neither_marker_is_unaffected_by_the_new_check() -> None:
    files = {"scripts/eval/unrelated_script.py": "print('no eval gateway here')\n"}
    assert _scan(files) == []
