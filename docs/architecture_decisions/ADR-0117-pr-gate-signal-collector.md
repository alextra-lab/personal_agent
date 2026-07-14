# ADR-0117: Deterministic signal collector for the PR gate — mechanize the mechanical, never the judgment

**Status:** Proposed
**Date:** 2026-07-14
**Deciders:** Owner (scope + the anti-caging constraint), master session (design, reflexive-infra author)
**Tags:** dev-process, master-gate, tooling, determinism, anti-caging, FRE-877

---

## Context

Master has two jobs at the delivery boundary: **schedule** work and **gate** PRs. Scheduling already has
a deterministic backbone — `scripts/reconcile_board.py` (the board does not lie) and
`scripts/dispatch/next_resolver.py --eligible` (who is NEXT) — so master schedules off scripts, not
recall. **Gating has no such backbone:** at the gate master gathers the signals by hand (run
`gh pr checks`, read the handoff comment, check mergeability, note dependabot), which is repetitive
legwork and easy to do inconsistently.

The obvious move — "make the gate a script" — has a trap this project has already paid for. A first pass
(the FRE-877 spec, `docs/specs/PR_GATE_DETERMINISTIC_PREFLIGHT.md`) proposed a *gate with opinions*: an
eight-check preflight that lints handoff completeness, infers codex mis-tiering, flags doc-drift and
seam-ownership, and classifies each as BLOCK or WARN. In review with the owner that design was **walked
back deliberately**, for one reason: **most of those checks are judgment wearing a mechanical costume.**
Linting whether a handoff is "complete," inferring whether codex ran "at the right tier," deciding
whether a `src` change "should" have touched MASTER_PLAN — each requires interpretation, and a script
that renders a verdict on interpretation **cages the very judgment master exists to apply.** This is the
ADR-0113 lesson in a new dress: that ADR's LLM-review harness hallucinated a security blocker on PR #433
and was falsified and removed. The danger of a gate is not missing something — it is **blocking
wrongly**, and every heuristic check is a wrongful-block waiting to happen.

**What needs deciding:** whether to mechanize the gate at all, and if so, where the line sits between
what a script may assert and what must remain master's free evaluation.

---

## Decision

Build a **signal collector, not a gate**: a master-run script that surfaces **only the unambiguous
external pass/fail signals** in one place, renders **no** judgment, makes **no** inference, and **never**
blocks. Everything that requires a thought stays master's, uncaged.

1. **The script emits only determinable yes/no signals** — raw facts an external system already reports,
   each mapped one-to-one to its source field, **never aggregated into a higher-level verdict**:
   - **Required CI checks** — the collector reads GitHub's *current* required check contexts for the PR's
     ruleset and emits each context's raw conclusion/status **separately**. It must **not** hardcode a
     check list or synthesize an overall "CI passed" — either would be the script *deciding* which checks
     matter or *aggregating* a verdict. If required-context discovery is unavailable, it emits `UNKNOWN
     required_checks` plus whatever raw check runs `gh` returns.
   - **Mergeability** — the raw GitHub fields, **not** a collapsed yes/no: `mergeable`,
     `merge_state_status`, `is_draft` (and any conflict flag), each verbatim; `UNKNOWN` for
     null/still-computing.
   - **Author identity** — `is_dependabot_author` as a raw boolean. It carries **no** merge implication
     (whether a dependabot bump is safe is master's judgment per lifecycle-rules § Signal trust boundary).
   It reads these from `gh` natively. That is the whole scope — raw fields only, no derived status.

2. **Everything requiring evaluation is master's, and is explicitly out of the script:** whether the work
   meets the backing ADR's *objective*; whether the AC evidence is real and at the right altitude;
   **codex** — whether it ran, at the right tier, and whether its findings were truly resolved (a review
   *with reasoning*, never a boolean); handoff completeness and quality; doc-drift; seam ownership;
   fold-in judgment; deploy classification; and the merge decision itself. The script must not touch any
   of these — not even to advise — because a mechanical opinion on a judgment call is an anchor on the
   judgment.

3. **Out of collector scope is NOT out of master's gate.** Removing these from *automation* does not
   weaken the gate. `lifecycle-rules.md` § Signal trust boundary and `/master` Step 2/4 keep their
   binding force: a Standard/Complex `src` diff with no codex review, a real-logic diff missing per-AC
   evidence or the self-review summary, unreconciled doc-drift, an unowned seam — each remains a master
   **bounce**, as a human-in-the-loop judgment. The collector merely refuses to *decide* them; master
   still must.

4. **UNKNOWN is first-class.** If a signal cannot be determined (an API is unreachable, a check has not
   reported), the script says so — it never renders a missing signal as PASS, and never as a block
   either. It reports facts and non-facts; master decides.

5. **Exit code never means "do not merge."** A successful collection exits 0 regardless of the signal
   values (PASS / FAIL / UNKNOWN all exit 0); nonzero is reserved **only** for a CLI usage error or an
   unhandled crash — never for red CI, a conflict, dependabot identity, or unknown external state. Wired
   as Step 4's first action, a nonzero-on-red-CI would otherwise become an accidental operational stop —
   a gate by the back door. The collector reports; it does not halt.

6. **The handoff template stays — as a reading aid, not a machine gate.** The fixed-header handoff
   (build SKILL Step 9 / adr SKILL Step 6, "the handoff contract") helps master read a close-out fast and
   completely. Its completeness is master's to judge, exactly as before; the script does not lint it.

7. **Master-run, wired into master SKILL Step 4** as the first action at the gate — a legwork-saver that
   populates the determinable facts so master's attention goes entirely to evaluation. It runs on
   master's own PRs too (closing the "master-authored PR has no signal view" gap noted in lifecycle-rules
   § Signal trust boundary), since master authoring reflexive-infra PRs is now routine.

The script's relationship to master is the same as `reconcile_board.py`'s: **it mechanizes the
mechanical so judgment is not spent gathering — it never substitutes for judgment.** A run tells master
"here are the hard facts"; master evaluates everything else freely.

---

## Alternatives Considered

### Option 1: Status quo — master gathers every signal by hand

**Description:** No tooling; master runs `gh pr checks`, reads comments, checks mergeability each gate.
**Pros:** zero build; nothing can cage judgment because there is no script.
**Cons:** repetitive legwork on the mechanical part; inconsistent (a signal can be skipped under context
pressure). **Why rejected:** this ADR replaces the *legwork*, not the judgment — the collector removes
the tax without touching the thinking.

### Option 2: The opinionated preflight (the walked-back spec)

**Description:** The original FRE-877 spec — eight checks including handoff-completeness linting, codex
mis-tier inference, doc-drift and seam heuristics, each BLOCK or WARN.
**Pros:** catches more mechanically; enforces the handoff contract.
**Cons:** **most of its checks mechanize judgment**, and a script that blocks on a heuristic *will* block
wrongly (the ADR-0113 failure mode) — eroding trust until the gate is ignored or ripped out. It also
inverts the master model: master exists to *judge*, and a gate that pre-judges cages it.
**Why rejected:** the owner's constraint — *the script may only assert determinable signals; the rest
must stay master's free evaluation; we must not cage the model* — is the whole point. Retained here only
as the design we deliberately did not build.

### Option 3: LLM-judge gate

**Description:** An LLM evaluates the PR against the criteria and returns a verdict.
**Pros:** could "judge" the soft checks.
**Cons:** **already falsified** — ADR-0113's LLM-review harness hallucinated a blocker on PR #433 and was
removed. A reasoning gate is untrustworthy precisely where it matters. **Why rejected:** the reasoning
stays with master (who can be reconsidered and corrected), never baked into an autonomous verdict.

---

## Consequences

### Positive
- Master's **mechanical legwork at the gate collapses to one call**; attention goes to evaluation.
- **No wrongful-block risk** — the script does no heuristics, so it cannot mis-judge; all judgment (where
  wrongness lives) stays with master, where it is revisable.
- **Simple to build and maintain** — it reads `gh`; no codex plumbing, no Linear parsing, no template
  linter.
- Gives master a signal view on **its own** PRs, which the worker self-fix loop never covered.

### Negative
- The collector **guarantees less** than the walked-back gate: it does not enforce handoff completeness,
  catch a mis-tiered codex, or flag drift. **This is intended** — those are master's to catch, and a
  determinable-signal collector is a *convenience, not a safety net*. The trade is deliberate: a smaller,
  trustworthy tool over a larger one that cages.
- Master must still do the full evaluation every gate; the script does not shrink that surface (nor
  should it).

### Risks and Mitigations
| Risk | Severity | Mitigation |
|------|----------|------------|
| Scope creep re-grows an opinionated gate over time | Medium | This ADR's line is the contract: **determinable external signals only.** A proposed check that requires interpretation is out by definition. |
| A missing signal read as PASS | High | UNKNOWN is first-class (Decision 3) — never silently PASS. |
| Master leans on the collector as if it were a gate | Medium | Wording + SKILL Step 4 framing: it reports facts; the evaluation (and the merge decision) remain master's. |

---

## Verification / Acceptance Criteria

- **AC-1 (signals accurate):** for a known PR, the collector reports each required CI check's pass/fail,
  dependabot status, and mergeability matching `gh`'s own view. *Check:* run against a known-green PR and
  a known-red PR; the output matches reality per signal.
- **AC-2 (no judgment, no block, no derived status):** the collector's output is **facts only**. *Check
  (output-schema + diff audit):* (a) no top-level `pass` / `fail` / `ready` / `blocked` / `warn` /
  `hold` / `merge` / `recommendation` field or string; (b) every status field maps one-to-one to a named
  external source field; (c) no code path combines two or more signals into a derived status, parses a
  handoff comment, infers a codex tier, or emits a merge/hold recommendation; (d) its exit code never
  means "do not merge" — exit 0 for all signal values (per Decision 5).
- **AC-3 (UNKNOWN first-class):** an undeterminable signal (unreachable API, unreported check) is
  reported as UNKNOWN, never PASS. *Check:* simulate a missing/unreported check; output shows UNKNOWN.
- **AC-4 (wired + own-PR):** master SKILL Step 4 runs the collector first; it works on a master-authored
  PR. *Check:* the SKILL step names it; a run against a master PR returns its signals.
- **AC-5 (handoff template as reading aid):** build SKILL Step 9 / adr SKILL Step 6 emit the fixed-header
  handoff; the collector does **not** read it. *Check:* the template exists; the collector has no code
  path that touches Linear comments.

**Seam owner:** master owns the assembled intent — the collector plus the SKILL Step 4 wiring plus the
handoff template land together and are verified against real PRs before this ADR is Implemented.

---

## References

- `docs/specs/PR_GATE_DETERMINISTIC_PREFLIGHT.md` — the first-pass spec (the opinionated design walked
  back here); this ADR supersedes its BLOCK/WARN taxonomy with the determinable-signal-only scope.
- `.claude/skills/lifecycle-rules.md` § Signal trust boundary — what master trusts vs re-checks; this ADR
  tools the *mechanical* half of it.
- `scripts/reconcile_board.py` · `scripts/dispatch/next_resolver.py` — the scheduling-side deterministic
  backbone this mirrors for the gating side.
- `docs/architecture_decisions/ADR-0113-*.md` — Superseded; the falsified LLM-review harness (hallucinated
  blocker on PR #433) — the anti-pattern that bounds this ADR.
- FRE-877 — tracking ticket (ADR-first per owner).

---

## Status Updates

### 2026-07-14 — Proposed
**Changed By:** Owner + master session
**Reason:** Master's gating job lacks the deterministic backbone its scheduling job has. The owner set the
binding constraint — the script may assert only determinable external signals; all evaluation stays
master's, uncaged — walking back the opinionated first-pass spec. Proposed for a minimal signal collector
on that basis.

---

**Template Version:** 1.1
**Based On:** [Michael Nygard's ADR pattern](https://cognitect.com/blog/2011/11/15/documenting-architecture-decisions)
