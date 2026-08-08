# FRE-1196 — Write the `/explore` working skill

**Ticket:** [FRE-1196](https://linear.app/frenchforest/issue/FRE-1196) · `Approved` → `In Progress`
**Backing ADR:** [ADR-0135](../../architecture_decisions/ADR-0135-explore-seat-working-contract.md) — D3, D4, D5, D6 (explore-side half)
**Seam:** FRE-1195 (adjudicates the ADR's own criteria — **not** this ticket's)
**Branch:** `fre-1196-explore-working-skill`

---

## Scope

Create `.claude/skills/explore/SKILL.md` — the working skill the explore seat has never had, the
counterpart to `prime-explore` and the peer of `build/SKILL.md` and `adr/SKILL.md`.

**In scope (this ticket):** the skill document, plus a contract test that guards its load-bearing
structure.

**Explicitly NOT in scope** — these are sibling tickets this one blocks, and touching their files
here would be scope creep:

| Not mine | Whose |
|---|---|
| `lifecycle-rules.md` amendments 1–7, `prime-explore` amendments 8–10 | FRE-1197 |
| `master/SKILL.md` D6 disposition step, four `basis` values | FRE-1198 |
| `next_resolver.py` / `launcher.py` / `gating_watcher.py` `stream:explore` wiring | FRE-1199 |
| ADR-0135's own acceptance criteria | seam FRE-1195 (ADR-0130 D1/D2) |

The skill's NEXT-resolution step names `--stream explore`; the route it names becomes real when
FRE-1199 lands. Writing the step is explore-side contract (D4), not dispatch code.

## The six areas the skill must cover (ticket body)

1. **Read-only on everything operational**, with exactly two bounded exceptions: `Backlog` tickets
   and comments (D5), and its own research-document branch and PR (D6).
2. **Measure against the live system; never reason from code** — as contract, not habit.
3. **The three-arm admissibility rule** for negative findings (D3), with the **UNVERIFIABLE** verdict.
4. **The fixed deliverable shape** — per finding: verdict · query · actual output · and, when the
   verdict is negative, the three arms. Plus a method appendix, a **Proposals** section that is the
   single place recommendations appear and holds **at most ten**, and a **filed-tickets list** naming
   every ticket the study filed.
5. **The durable substrate map** — `_count` not `_cat`; ES counts provisional per FRE-1051; per-call
   series authority is Postgres `api_costs`, not Elasticsearch (per-call emit dark since 2026-05-10);
   config read from the running process, not the repo; deployed code identified by container file
   hash, not board state.
6. **Branch and path write scope** — `docs/research/<date>-fre-XXXX-<slug>.md` on branch
   `explore-fre-XXXX-<slug>`, nothing else, and never merge.

## The load-bearing design decision: deletion-separable arms

Seam AC-2 runs a **discrimination test** on this text: apply the rule verbatim to FRE-1131 §F1 row B
(`within_session_compressed` = 0) — it must be **inadmissible**; then apply *the same text with arm 1
deleted* — it must be **admissible**. That test is only runnable if the deletion is well-defined and
the remainder is still a rule.

Three constraints follow, and they are the reason for the structure below:

- **Explicit block anchors.** Each arm is wrapped in `<!-- ARM-n:START -->` / `<!-- ARM-n:END -->`
  HTML comments (invisible when rendered), so "delete arm 1" is a mechanical, unambiguous operation
  rather than an editorial judgment.
- **No hardcoded count in the operative sentence.** The rule reads "**every arm stated below**", not
  "all three of". A count in the operative sentence would make the arm-1-deleted variant
  self-contradictory instead of merely shorter. The count appears only in surrounding commentary,
  outside the `<!-- RULE:START -->` block.
- **No cross-arm references.** Arm 2 may not say "as in arm 1"; each arm states its own test in full,
  so any one can be removed without stranding a dangling reference.

A short *maintenance note* in the skill states these three constraints, so a later editor does not
undo them by tidying.

## Worked example (ticket AC-3)

Written into the skill: FRE-1131 §F1, the real historical failure.

| Mechanism | Fire event | Count since 07-23 |
|---|---|---|
| B — within-session hard gate (ADR-0061) | `within_session_compressed` | **0** |
| D's per-turn evaluator (FRE-944) | `cache_reset_decision` | **94** |

Row B is a negative carrying **only** a same-store liveness control (row D: identical index, window
and shape, non-zero). Applying the rule as written: arm 2 ✓, arm 3 ✓ (index and window are named),
arm 1 ✗ — no raw `agent-logs` document exhibits `within_session_compressed`, and the identifier's only
real producer is the event-bus stream `stream:context.within_session_compressed`
(`telemetry/within_session_compression.py`), which does not feed the queried store. **Verdict:
inadmissible for want of arm 1.**

The example then shows both honest exits, because that is what makes the rule usable rather than
merely restrictive:

- **Search for arm 1 first.** Here it changes the answer: the real emits are
  `within_session_compression_hard_trigger` / `within_session_compression_recorded`
  (`telemetry/within_session_compression.py:137`), whose counts are 260 all-time / 2 since 2026-07-20.
  The finding was never negative — it was a *positive* read off a wrong identifier.
- **If no arm-1 form can be produced at all** — no instance, no producer for that identifier in that
  store — the verdict is **UNVERIFIABLE**, not negative.

## The UNVERIFIABLE separating test (ticket AC-4)

Stated as a three-branch test a reader can apply, so the two verdicts are separated by a check rather
than merely both named:

- arm 1 **can be produced, and is** → **NEGATIVE** (admissible once arms 2 and 3 are also carried);
- arm 1 **cannot be produced** after search — neither a raw instance nor a producer for that
  identifier in that store → **UNVERIFIABLE**; it asserts nothing about whether the thing happens;
- arm 1 **has not been attempted** → no verdict yet, only an unfinished measurement. Neither value is
  available.

---

## Steps

Each step names its verification. Test-first: the contract test is written and observed failing
before the skill exists.

| # | Step | File | Verify |
|---|---|---|---|
| 1 | Write the contract test module | `tests/scripts/test_explore_skill_contract.py` | `uv run pytest tests/scripts/test_explore_skill_contract.py` → **all fail** (no skill file) |
| 2 | Write the skill | `.claude/skills/explore/SKILL.md` | same command → **all pass** |
| 3 | Full module + suite | — | `make test-file FILE=tests/scripts/test_explore_skill_contract.py`, then `make test` |
| 4 | Quality gates | — | `make mypy` · `make ruff-check` · `make ruff-format` · `pre-commit run --all-files` |
| 5 | Self-review (Step 8) | — | `feature-dev:code-reviewer` on `git diff origin/main...HEAD`; security-review not triggered (no inputs/subprocess/auth/network in the diff) |
| 6 | PR + handoff comment | — | PR opened; `save_comment` on FRE-1196 |

### Step 1 — the contract test (what it actually asserts)

Precedent: `tests/scripts/test_dispatch_skill_contracts.py` — content guards on stable markers, not
brittle exact-prose. Same shape here. Tests, one per this ticket's own acceptance criteria:

- `test_skill_file_exists_with_frontmatter` — file at `.claude/skills/explore/SKILL.md`; frontmatter
  block present with `name: explore` and a `description:` line.
- `test_covers_all_six_contract_areas` — one stable marker per area (read-only scope · measure-live ·
  admissibility rule · deliverable shape · substrate map · branch/path scope).
- `test_arms_are_separately_delimited` — all three `ARM-n:START`/`END` anchor pairs present, in order,
  non-overlapping, nested inside `RULE:START`/`RULE:END`.
- `test_deleting_arm_one_leaves_a_complete_rule` — **performs the deletion** on the file text: excise
  `ARM-1:START…ARM-1:END`, then assert the remainder still contains the operative sentence, arms 2 and
  3 intact, and that the operative sentence contains **no** hardcoded arm count (the failure mode the
  anchors exist to prevent).
- `test_arms_do_not_cross_reference` — no arm block's body refers to another arm by number.
- `test_worked_example_returns_inadmissible` — the worked-example section names
  `within_session_compressed`, names the same-store liveness control, and states the verdict
  **inadmissible**.
- `test_unverifiable_is_separated_by_a_stated_test` — `UNVERIFIABLE` appears, and its section states
  the arm-1-cannot-be-produced branch that converts a negative into it.
- `test_deliverable_shape_names_proposals_cap_and_filed_tickets` — the deliverable-shape section names
  the Proposals section, the cap of ten, and the filed-tickets list, each in requirement voice
  (`MUST`/`never`/`inadmissible`), not as a suggestion.
- `test_filing_is_backlog_only` — D5: `Backlog` only, never `Needs Approval`, never self-promote.

### Risk tier (build SKILL § 3)

**Standard.** No `src/` logic, no schema, no cost/governance code — but it is a **new ADR's
implementation** and the wording is load-bearing (a seam criterion reads this exact text). Codex
plan-review runs; owner approval before coding.

### Diff class (build SKILL § 8)

**Self-serve.** Process/skill wording plus a read-only test module. No production write path, nothing
destructive, no schema change, no cost or governance code. It does not change the trust ladder in
`docs/plans/OWNER_CONSOLE.md`.

---

## Acceptance criteria — this ticket's own (from the ticket body)

| # | Criterion | How it is proven |
|---|---|---|
| AC-1 | The skill file exists at `.claude/skills/explore/SKILL.md` with frontmatter naming it, and loads a contract covering all six areas. Fails if any area is absent. | `test_skill_file_exists_with_frontmatter` + `test_covers_all_six_contract_areas` |
| AC-2 | The three-arm rule is written so deleting arm 1 leaves arms 2 and 3 syntactically intact and independently applicable — verified by performing that deletion on a copy. Fails if the arms are interleaved. | `test_arms_are_separately_delimited` + `test_deleting_arm_one_leaves_a_complete_rule` + `test_arms_do_not_cross_reference` — the deletion is actually executed on the file text |
| AC-3 | Applying the rule as written to a negative finding carrying only a same-store liveness control returns **inadmissible**, demonstrated on one worked example in the skill. | `test_worked_example_returns_inadmissible` + the worked example read end to end |
| AC-4 | UNVERIFIABLE is stated and separated from a negative verdict by an applicable stated test — arm 1 unsatisfiable ⇒ UNVERIFIABLE. | `test_unverifiable_is_separated_by_a_stated_test` |
| AC-5 | The deliverable-shape section names all three of the Proposals section, its cap of ten, and the filed-tickets list, each as a requirement. | `test_deliverable_shape_names_proposals_cap_and_filed_tickets` |
