# ruff: noqa: D103
"""Contract guards for the FRE-806 orchestrated-dispatch skill changes.

The deliverable is process-doc (SKILL.md) behaviour the model executes, so these
are targeted content guards on stable markers — not brittle exact-string prose
matches, and not behaviour tests (the launcher's ticket-seed is behaviour-tested
in test_launcher.py). They pin the acceptance-criteria invariants:

  AC1 — prime-worker no longer resolves NEXT / surfaces a dispatch card.
  AC2 — the self-fix path triggers on master-bounce OR CI-red (same shape).
  AC4 — the build and adr skills do NOT arm a polling loop (removed 2026-07-06, FRE-822).
"""

from __future__ import annotations

from pathlib import Path

_SKILLS = Path(".claude/skills")


def _read(rel: str) -> str:
    return (_SKILLS / rel).read_text()


def _norm(text: str) -> str:
    return " ".join(text.lower().split())


# --- AC1: prime-worker sheds resolution + advise ---------------------------


def test_prime_worker_no_longer_resolves_next() -> None:
    text = _norm(_read("prime-worker/SKILL.md"))
    # The orchestrator owns resolution now — these markers must be gone.
    assert "resolve next from linear" not in text
    assert "head of queue" not in text
    assert "surface the dispatch card" not in text


def test_prime_worker_is_a_pure_pr_monitor() -> None:
    text = _norm(_read("prime-worker/SKILL.md"))
    assert "pr-feedback monitor" in text


# --- AC2: self-fix triggers on master-bounce OR CI-red ---------------------


def test_self_fix_triggers_on_bounce_and_ci_red() -> None:
    text = _norm(_read("prime-worker/SKILL.md"))
    # both triggers named, same fix-mode
    assert "master gate — bounce" in text or "master gate -- bounce" in text
    assert "red ci" in text or "ci-red" in text or "ci red" in text
    # the CI-red ack marker (SHA-keyed dedup) and the never-merge bound
    assert "addressing red ci" in text
    assert "never merge" in text


# --- AC4: build + adr no longer arm a polling loop (FRE-822 cache-TTL fix) --
# The 20m /prime-worker cron fired past the 5-min prompt-cache TTL every tick,
# re-reading the full session context uncached. Removed — the owner triggers the
# worker's PR-feedback check on demand (/prime-worker), same logic, no cron.


def test_build_skill_arms_no_loop() -> None:
    text = _norm(_read("build/SKILL.md"))
    assert "/loop 20m /prime-worker" not in text  # no polling cron armed
    assert "no monitor loop" in text  # on-demand: owner re-runs /prime-worker


def test_adr_skill_arms_no_loop() -> None:
    text = _norm(_read("adr/SKILL.md"))
    assert "/loop 20m /prime-worker" not in text
    assert "no monitor loop" in text


# --- AC-1 (skill-wiring half, FRE-832): prime-master reads the trigger ledger --
# The durable-read mechanism (trigger_ledger.main, FRE-832) is unit-tested in
# test_trigger_ledger.py; this pins that prime-master's rebuild step actually
# calls it and surfaces the result, so an edit that drops the wiring fails CI
# rather than only a human re-read of the skill file.


def test_prime_master_reads_trigger_ledger_on_rebuild() -> None:
    text = _norm(_read("prime-master/SKILL.md"))
    assert "python -m scripts.dispatch.trigger_ledger --unconsumed --json" in text
    assert "unconsumed actuation trigger" in text
