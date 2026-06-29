# FRE-680 — Delivery verification spine v1 (board reconciler + direct-to-main guard hook)

> Linear: FRE-680 (Approved, Tier-2:Sonnet) · one-off dev-harness improvement, no project, no follow-on chain.
> Self-classified **Standard** (new script logic + security-adjacent guard hook + multi-file) → codex plan-review + owner approval before coding.

## Goal

Convert two remembered guardian rules into mechanical checks:
1. A **deterministic, read-only board reconciler** that maps board claims to durable evidence and emits verdicts.
2. A **PreToolUse guard hook** that blocks a direct push to `origin main` from a build/worktree branch, with an allow path for master pushing docs from main.
3. Document the evidence contract in the shared `lifecycle-rules.md` (authorized edit).

## Acceptance criteria (from ticket) → how each is proven

| AC | Proof |
|----|-------|
| AC1 — reconciler flags the FRE-655 drift (header narrates In Progress, Linear shows closed) | Run `python scripts/reconcile_board.py` with Linear reachable → a `FAIL` verdict whose claim names FRE-655, evidence cites `MASTER_PLAN.md:5` + `Linear FRE-655 status=Done`. Captured in ticket comment. |
| AC2 — verdict schema is exactly the four fields; UNVERIFIABLE never collapses to PASS | Unit test asserts each verdict dict has exactly `{claim, status, evidence, note}`, `status ∈ {PASS, FAIL, UNVERIFIABLE}`, and a no-source path yields `UNVERIFIABLE` (never PASS). |
| AC3 — simulated push to origin main from a build branch blocked; docs push from main via allow path passes; hook test green | `bash .claude/hooks/test_block_direct_main_push.sh` → `ALL PASS`. |
| AC4 — lifecycle-rules.md contract subsection present with exact prose | `grep` the verbatim prose under the new `### Evidence contract (proof of Done)` subsection. |
| AC5 — `make ruff-check` + `make ruff-format` clean on new script; no changes under `src/personal_agent` | Run both; `git diff --name-only` shows nothing under `src/personal_agent/`. |

## Deliverable 1 — `scripts/reconcile_board.py` (read-only, no LLM)

A standalone CLI. Reconciles MASTER_PLAN narrative ↔ Linear state ↔ merged-PR/git evidence. **Verdict dataclass = exactly four fields:** `claim: str`, `status: Literal["PASS","FAIL","UNVERIFIABLE"]`, `evidence: list[str]`, `note: str`.

**Data sources & access:**
- **Linear state** — Linear GraphQL API (`https://api.linear.app/graphql`) via **stdlib `urllib.request`** (no `requests`/types-requests dependency), using `AGENT_LINEAR_API_KEY`. Key resolution: env var first; else parse a `.env` at the git toplevel (`git rev-parse --show-toplevel`). If neither yields a key, or the request fails → Linear-dependent verdicts become **UNVERIFIABLE** (never PASS). No hardcoded absolute paths (pre-commit `check_no_personal_paths.py`).
- **Merged PRs** — `gh pr list --state merged --search "<fre-id>" --json number,headRefName,mergedAt`. If `gh` unavailable/errors → **UNVERIFIABLE**.
- **MASTER_PLAN narrative** — parse `docs/plans/MASTER_PLAN.md`, scoped to the `> **Last updated**:` header paragraph (where current-state narration lives; AC1 says "the header narrates").

**Header claim extraction (codex-revised — explicit current-state phrase wins, NOT proximity aggregation):**
1. Isolate the header block (the `> **Last updated**:` paragraph), split into **clauses** on sentence/clause delimiters (`. ` / `; ` / ` — ` / `·` / `)` / newlines).
2. For each clause containing a `FRE-\d+`, bind state from phrases **in that clause**, with a strict priority:
   - **Current-OPEN (authoritative)** — present-tense assertions of ongoing openness: `stays In Progress`, `kept In Progress`, `held In Progress`, `stays OPEN`, `kept OPEN`, `kept OPEN (NOT closed)`, `closes only when`, or a bare `In Progress` **not** preceded by past-tense `was`/`were`. → claimed **OPEN**.
   - **DONE** — `Done`, `DONE`, `SHIPPED`, `DEPLOYED`, `closed Done`, `merged`. → claimed **DONE**.
   - else → no binding from this clause.
3. Aggregate per ticket: if **any** clause yields a Current-OPEN binding → claim **OPEN** (a deliberate current assertion master wrote); else if any DONE → claim **DONE**; else **OTHER** (skip, no verdict). *(OPEN-priority, not DONE-priority — "stays In Progress" is a knowing current claim; past-tense "was In Progress" is excluded so a "was In Progress … shipped" string classifies DONE, not a false OPEN-drift. FRE-655's header has "(FRE-655 stays In Progress)" → OPEN.)*
4. **Check A (MASTER_PLAN ↔ Linear):**
   - Linear unreachable → UNVERIFIABLE.
   - claimed OPEN, Linear `Done`/`Canceled` → **FAIL** (drift — this is AC1 for FRE-655).
   - claimed DONE, Linear not Done → **FAIL** (reverse drift).
   - claim matches Linear → PASS.
4. **Check B (Done ↔ merged-PR):** for tickets Linear marks Done (or claimed DONE), query merged PRs.
   - merged PR with branch containing the fre-id found → PASS (evidence: PR #, headRef, mergedAt).
   - gh unreachable → UNVERIFIABLE.
   - no PR found → UNVERIFIABLE (note: "no merged PR found — may be decision-only or branch-naming"). *(Deliberately not FAIL: cannot cleanly prove a negative; reserve FAIL for the unambiguous MASTER_PLAN↔Linear contradiction. Keeps UNVERIFIABLE first-class per AC2.)*

**Output:** human-readable table to stdout by default; `--json` prints the verdict list as JSON (for prime-master / master post-merge). Exit code `1` if any `FAIL` (so callers can detect drift), `0` otherwise — consistent with repo checkers (`check_no_personal_paths.py`).

**Standards:** Google docstrings; modern type hints; frozen dataclass for the verdict; `requests` (already a dep) for GraphQL; `subprocess` for `git`/`gh`. `print()` is fine (CLI under `scripts/`, not `src/personal_agent/`).

## Deliverable 2 — `.claude/hooks/block-direct-main-push.sh` (PreToolUse, matcher Bash)

Mirrors `deploy-approval-gate.sh` structure. Role determined by worktree root of CWD; **push-target analysis done in embedded python3** (argv tokenization via `shlex`, not bash substring matching — codex concern #3).

```
input=$(cat); cmd=<extract tool_input.command via python json>
# only inspect `git push`
if argv is not a git push: exit 0
# classify destination by tokenizing argv:
#   targets_main = any refspec arg resolves to main:
#       main | main:main | HEAD:main | <src>:main | refs/heads/main | HEAD:refs/heads/main | <src>:refs/heads/main
#   explicit_feature = a refspec arg whose DEST is a non-main branch (e.g. fre-680-x, src:fre-x)
#   bare = no refspec arg (git push | git push origin | with only flags/remote)
root = git toplevel (or pwd)
case root in
  */.claude/worktrees/build|*/build2|*/adrs)         # build/worktree roles
      if targets_main: BLOCK (exit 2)
      elif explicit_feature: exit 0                  # normal feature-branch push (build Step 9)
      elif bare: BLOCK (exit 2, fail-closed)         # codex #2: unresolved dest in a worktree → deny
      else: exit 0
  *)  exit 0                                          # primary tree = master's domain (allow path: docs/MASTER_PLAN from main)
esac
```

Block message: `"BLOCKED: direct push to main is forbidden from the build/worktree session (role boundary). Open a PR; master merges. (FRE-680 guard)"`. The allow path is the primary tree (where `main` is checked out) — master pushes docs/MASTER_PLAN directly. Worktrees may push their own feature branches but never main, and a bare/unresolvable push from a worktree fails closed.

Wire into `.claude/settings.json` `PreToolUse` → `Bash` hooks array, after `deploy-approval-gate.sh`.

**Test:** `.claude/hooks/test_block_direct_main_push.sh` — **hermetic** (codex #5): builds `mktemp -d` dirs with `.claude/worktrees/{build,adrs}` suffixes (and a plain primary dir); no real repo, no hardcoded paths. Cases:
1. `git push origin main` from build-shaped dir → BLOCK (2)
2. `git push origin HEAD:main` from adr-shaped dir → BLOCK (2)
3. `git push origin refs/heads/main` from build-shaped dir → BLOCK (2)
4. `git push origin main:main` from build-shaped dir → BLOCK (2)
5. `git push --force-with-lease origin fre-680-x` from build-shaped dir → ALLOW (0)
6. `git push origin feature-mainline` from build-shaped dir → ALLOW (0)  *(branch name contains "main" — must not false-block)*
7. bare `git push` from build-shaped dir → BLOCK (2)  *(fail-closed)*
8. `git push origin main` from primary dir → ALLOW (0)  *(allow path / docs)*
9. `git status` from build-shaped dir → ALLOW (0)  *(non-push)*

## Deliverable 3 — `.claude/skills/lifecycle-rules.md`

Under `## Ticket state`, add a new subsection (authorized shared-skill edit), **verbatim** (the ticket mandates exact prose for AC4 — kept as-is even though it states the contract more absolutely than the script's conservative Check B, which is intentional: the doc states the ideal, the script is conservative/UNVERIFIABLE on no-PR — codex #4):

> ### Evidence contract (proof of Done)
>
> A ticket is Done only when its claim maps to durable evidence. Done means a merged PR whose branch maps to the ticket (fre-XXX); if the ticket cites a backing ADR with acceptance criteria, those are separately proven. A MASTER_PLAN narrative state must match current Linear state plus merged-PR evidence. Deployed-at-SHA means git log of main equals the claimed SHA and health is green. UNVERIFIABLE (no source to check) is a first-class verdict, never silently treated as PASS. scripts/reconcile_board.py is the deterministic check.

## TDD step order

1. `tests/test_scripts/test_reconcile_board.py` — failing tests for: (a) verdict schema = exactly 4 fields + UNVERIFIABLE≠PASS (AC2); (b) header parsing classifies "(FRE-655 stays In Progress)" as OPEN; (b2) **adversarial fixture** `"FRE-655 was In Progress … FRE-655 shipped"` classifies DONE (not a false OPEN-drift) — codex #1; (c) Check A logic: claimed-OPEN + injected Linear-Done → FAIL; injected Linear-unreachable → UNVERIFIABLE (pure-function tests, no network). Confirm fail → implement script → green.
2. `.claude/hooks/test_block_direct_main_push.sh` — write, confirm fail (no hook), implement hook, green.
3. Wire settings.json; append lifecycle-rules subsection.
4. Live AC1: export `AGENT_LINEAR_API_KEY` from primary `.env` (ad-hoc, not committed), run reconciler, capture FRE-655 FAIL verdict.

## Test commands

- `make test-file FILE=tests/test_scripts/test_reconcile_board.py`
- `bash .claude/hooks/test_block_direct_main_push.sh`  → `ALL PASS`
- `make ruff-check` · `make ruff-format` · `make mypy` (script only) · `pre-commit run --all-files`
- AC1 live: `AGENT_LINEAR_API_KEY=… python scripts/reconcile_board.py` → FRE-655 FAIL verdict

## Out of scope (v1, per ticket)

Judgment-layer verifier agents (acceptance-verifier, adr-grounding-verifier) and the synthesized merge-verdict card. No `src/personal_agent/` changes.
