# ruff: noqa: D103
"""Contract guards for the master/build/adr dispatch skill model.

Content guards on stable markers (not brittle exact-prose or behaviour tests).
They pin the invariants of the automation redesign (process/automation-redesign):

  - prime-worker is RETIRED — its self-fix is folded into the build/adr skill's
    "respond to a poke" behaviour; the standalone skill no longer exists.
  - build + adr do NOT arm a polling loop (FRE-822 cache-TTL fix), and their
    definition of *done* extends to master-ready (self-complete on a watcher CI
    poke or a direct master bounce).
  - prime-master reads the trigger ledger on rebuild.
  - build/adr/master resolve dispatch NEXT via the external resolver
    (`scripts/dispatch/next_resolver.py`, FRE-846), not inline Linear calls.
"""

from __future__ import annotations

from pathlib import Path

_SKILLS = Path(".claude/skills")


def _read(rel: str) -> str:
    return (_SKILLS / rel).read_text()


def _norm(text: str) -> str:
    return " ".join(text.lower().split())


def _section(text: str, start_marker: str, end_marker: str) -> str:
    """Slice `text` between `start_marker` and the next `end_marker` after it.

    Scopes an assertion to one paragraph/step instead of the whole file, so a
    stray match elsewhere in the skill can't hide a regression at the actual
    call site.
    """
    start = text.index(start_marker)
    end = text.index(end_marker, start)
    return text[start:end]


# --- prime-worker retired (folded into the build/adr skills) ----------------


def test_prime_worker_retired() -> None:
    # The standalone PR-feedback monitor is gone; workers self-complete instead.
    assert not (_SKILLS / "prime-worker" / "SKILL.md").exists()


# --- build + adr: no polling loop; done extends to master-ready -------------


def test_build_skill_no_polling_loop_and_self_completes() -> None:
    text = _norm(_read("build/SKILL.md"))
    assert "/loop 20m /prime-worker" not in text  # no polling cron armed
    assert "never arm a" in text and "/loop" in text  # loop explicitly forbidden
    assert "master-ready" in text  # done extends past the PR (CI-green + bounces)


def test_adr_skill_no_polling_loop_and_self_completes() -> None:
    text = _norm(_read("adr/SKILL.md"))
    assert "/loop 20m /prime-worker" not in text
    assert "never arm a" in text and "/loop" in text
    assert "master-ready" in text


# --- prime-master reads the trigger ledger on rebuild ----------------------
# The durable-read mechanism (trigger_ledger.main, FRE-832) is unit-tested in
# test_trigger_ledger.py; this pins that prime-master's rebuild actually invokes
# it, so an edit that drops the wiring fails CI rather than only a human re-read.


def test_prime_master_reads_trigger_ledger_on_rebuild() -> None:
    text = _norm(_read("prime-master/SKILL.md"))
    assert "trigger_ledger --unconsumed --json" in text
    assert "in-flight actuation" in text


# --- build/adr/master resolve dispatch NEXT via the external resolver (FRE-846)
#
# Scoped to the specific paragraph/step being rewritten (not file-wide) so the
# tests prove the OLD inline busy-guard/priority/blocked-by logic is gone from
# the actual call site, not merely absent somewhere or present somewhere else.


def test_build_skill_uses_external_resolver_for_stream_selector() -> None:
    section = _norm(_section(_read("build/SKILL.md"), "Argument:", "## Step 0"))
    assert "next_resolver --stream build" in section
    assert "list_issues(" not in section
    assert "includerelations" not in section


def test_adr_skill_uses_external_resolver_for_stream_selector() -> None:
    section = _norm(_section(_read("adr/SKILL.md"), "Argument: none", "## Step 0"))
    assert "next_resolver --stream adr" in section
    assert "list_issues(" not in section
    assert "includerelations" not in section


def test_master_skill_uses_external_resolver_for_advance_dispatch() -> None:
    section = _norm(_section(_read("master/SKILL.md"), "## 8 — Close out", "## Identity"))
    assert "next_resolver --stream" in section
    assert "--eligible" in section
    assert "list_issues(" not in section
    assert "includerelations" not in section
