# Spec — `pr_gate.py`: deterministic PR-gate preflight + handoff template

> Status: **Proposed** (spec for owner approval) · Author: master session · Date: 2026-07-14
> Relates to: `.claude/skills/lifecycle-rules.md` § Signal trust boundary · `scripts/reconcile_board.py`
> (the pattern) · `scripts/dispatch/next_resolver.py` · master SKILL Step 4 · ADR-0113 (the falsified
> LLM-review harness — the anti-pattern this must not repeat).

## Problem

Master's two jobs each need a deterministic backbone. **Scheduling already has one** —
`reconcile_board.py` (board truth: no lying states/relations) + `next_resolver.py --eligible` (the
NEXT). **Gating does not.** At the PR gate master today *eyeballs* the signals — CI, the handoff
comment, whether codex ran, PR hygiene — which is inconsistent (a field can be missed), slow, and
invites the exact failure the trust boundary forbids: re-deriving the build to re-confirm what a signal
already asserts.

The missing twin is a deterministic preflight that mechanically verifies every **checkable** signal is
present and consistent, fails loud with the specific gap, and thereby frees master's attention
*entirely* for the semantic residue only a human can judge.

## Design

### 1. `pr_gate.py` — a deterministic preflight (no LLM)

A script in `scripts/dispatch/` mirroring `reconcile_board.py`'s contract: input a PR number; emit a
machine result (exit code + JSON) and a human summary; **PASS** or a structured list of the exact gaps.
It performs **only mechanical checks** — string/state/diff assertions, no model, no judgment.

**Checks (each returns pass / fail-with-reason):**

| # | Check | Source | Fail means |
|---|-------|--------|------------|
| 1 | All *required* CI checks green | `gh pr checks` | red/pending required check |
| 2 | PR hygiene — checklist carries no post-deploy/deploy/"verify on prod" phrases | PR body scan | post-deploy item in a pre-merge checklist |
| 3 | Ticket state is In Progress or In Review | Linear | wrong state (integration drift or premature gate) |
| 4 | Branch ↔ ticket: `fre-XXX` head maps to a real ticket | branch name + Linear | unmappable head |
| 5 | **Handoff-contract completeness** — the master-addressed ticket comment contains every required field | Linear comment vs the template (§2) | a required handoff field is absent |
| 6 | **Codex mis-tier backstop** — if the diff touches `src/`-logic/schema/security/cost/memory, the handoff/PR must mention a codex plan-review | `git diff --name-only` + handoff scan | Standard/Complex diff with no codex signal |
| 7 | **Doc-drift prompt** — `src/` changed but no MASTER_PLAN / ADR-status change in the PR | diff paths | (advisory) possible drift to reconcile |
| 8 | **Seam prompt** — the backing ADR has other non-terminal children | Linear | (advisory) confirm who owns the assembled seam |

Checks 1–6 are hard (fail → not gate-ready). Checks 7–8 are advisory prompts (surface to master, never
auto-fail). Output mirrors `reconcile_board.py`: nonzero exit on any hard fail, JSON detail, one-line
human verdict.

### 2. The handoff template (what makes 5 & 6 deterministic)

The build/adr close-out is codified today as prose (build SKILL Step 9 / adr SKILL Step 6, "the handoff
contract"). Free prose is *not* reliably machine-checkable. Convert it to a **light structured
template** — fixed field headers the close-out fills — so check 5 becomes trivial field-presence and
check 6 a keyword scan. Required fields (unchanged in content, now headered):

- `Backing ADR + acceptance criteria` (the criteria this ticket implements)
- `AC evidence` (per criterion: named test / probe output / observed behaviour)
- `Reviews` (code-review effort + findings fixed/deferred; security-review verdict; codex tier + verdict)
- `Deploy class` (standing-class name or ask-first)
- `Post-deploy runbook` (steps, or "none")
- `Seam ownership` (this child closes the ADR? / who owns the assembled proof)
- `Context disposition` (keep / clear)

The template is emitted verbatim by the build/adr skills; `pr_gate.py` lints its presence, **not its
quality**.

### 3. The residue — what `pr_gate.py` deliberately does NOT check

These are irreducibly master's judgment; a PASS never implies them:

- Does the delivered thing meet the backing ADR's **objective** (not merely pass its tests).
- Is the AC **evidence at the right altitude** (the script sees a test is *named*; it cannot judge it
  *proves* the criterion).
- **Fold-ins** genuinely supporting vs scope creep.
- **Deploy classification** edge cases and the deploy-timing call.
- The **merge / deploy** decision itself (the one-way door).

### 4. Hard constraint (the ADR-0113 lesson)

`pr_gate.py` is **deterministic mechanical checks only — never an LLM judge.** ADR-0113's LLM-review
harness hallucinated a security blocker on PR #433 and was falsified and removed; a gate that reasons
is untrustworthy. The script guarantees *signals present + consistent*; the human (master) does the
judgment. **PASS = "signals complete + consistent," never "work correct."** The script must not shrink
the residue — it exists to point master's full attention *at* the residue.

### 5. Wiring

Master SKILL Step 4 runs `pr_gate.py <PR#>` as the first action at the gate. A hard-fail is a
bounce-or-fix before any human review (and closes the "master's own PRs have no red-CI signal" hole —
master runs it on its own PRs too). A PASS moves master straight to the residue check, then
merge/deploy/schedule.

## Acceptance criteria

- Each of checks 1–8 has a unit test (mocked `gh`/Linear/diff) proving pass **and** the specific
  fail/flag.
- Run against a known-good merged PR (e.g. #520) → **PASS**.
- Run against a PR with a missing handoff field → flags **exactly** that field (check 5).
- Run against an `src/`-diff PR whose handoff omits codex → flags mis-tier (check 6).
- Advisory checks 7–8 surface as prompts, never as a hard fail.
- Output contract matches `reconcile_board.py` (exit code + JSON + human line).
- The handoff template is emitted by build SKILL Step 9 / adr SKILL Step 6, and `pr_gate.py` lints it.
- Wired into master SKILL Step 4 as the preflight.

## Alternatives

- **Status quo (master eyeballs each signal).** Inconsistent, slow, invites re-derivation. Rejected —
  this spec exists to replace it.
- **LLM-judge gate.** Falsified by ADR-0113 (hallucinated blocker). Rejected as the anti-pattern.
- **Deterministic script (chosen).** The `reconcile_board.py` pattern, proven for the scheduling twin.

## Open decision for the owner

Does this go **ADR-first** (route to the `/adr` session — it's a gate-*process* change with a real,
already-falsified alternative, so it may merit the debate-first ADR treatment) **or straight to a build
ticket** (the design is largely settled here)? Master's lean: a **short ADR** — the LLM-vs-deterministic
decision and the "PASS ≠ correct" boundary are worth pinning durably — then the build ticket off it.
