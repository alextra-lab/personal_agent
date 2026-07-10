# Dev/Deploy Process v2 — Design

**Date:** 2026-07-04
**Status:** Draft — pending owner review
**Origin:** External DevSecOps review of the delivery pipeline (see `docs/reference/` delivery-pipeline doc, commit 60799b3) evaluated against ground truth of the actual environment. This spec records what we adopt, what we adapt, and what we reject — with reasons.

---

## 1. Context and ground truth

Facts verified 2026-07-04, correcting several of the review's assumptions:

- Repo `alextra-lab/personal_agent` is **public** → rulesets, environments with required reviewers, and merge queue are all available free. No plan constraint.
- A ruleset named **"Main"** already exists (active, readable via API — the review's 403 applied only to classic branch-protection endpoints). It currently enforces only **deletion + non-fast-forward** protection. No PR requirement, no required checks.
- **No GitHub environments** exist; deploys do not go through GitHub. The guardian (master session) runs `ENV=cloud make rebuild SERVICE=...` on the VPS directly.
- Linear team **FrenchForest** states: Backlog, Needs Approval, Approved, Todo, In Progress, In Review, Done, Duplicate, Canceled. **No post-merge state.** The GitHub integration auto-moves issues to Done on PR merge — the confirmed false-Done bug (bit us 2026-07-02: merged PRs closed deploy-held tickets).
- CI is a single `ci.yml` triggering on **every push to any branch and every pull_request** — PR branches run the full suite twice per push. All jobs run unconditionally (PWA e2e runs on backend-only changes, etc.). Checks on main: Backend unit, Backend integration (transport), Lint (mypy+ruff), PWA unit (Vitest), PWA e2e (Playwright), Telemetry surface reconciliation (gate), CodeQL Analyze ×3 + aggregate.
- VPS: 8 vCPU (Haswell), 22 GiB RAM (~10 GiB free), running the prod `cloud-sim-*` stack plus one shared test stack (`seshat-*-test-1`, ~2.5 GiB). The one-pytest-at-a-time lock exists for **CPU/memory saturation**, not only DB collisions.
- All sessions (master, build1, build2, adr) authenticate to GitHub as the owner's single identity.

## 2. Decisions (owner-confirmed 2026-07-04)

| # | Area | Decision |
|---|------|----------|
| D1 | Deploy execution | **Stays guardian-run on the VPS.** No GitHub environments, no self-hosted runner. |
| D2 | Linear states | **+2 states**: `Awaiting Deploy` (normal post-merge), `Verify Failed` (exception only). |
| D3 | Dispatch | **Linear-native** (labels + priority + blocked-by relations). MASTER_PLAN becomes strategy-only. |
| D4 | Ruleset | **C1: required status checks on main**; docs flow moves to auto-merge PRs. |
| D5 | CI | **Dedupe triggers + path-aware in-job skips.** No merge queue, no nightly lane yet. |
| D6 | Substrate isolation | **Deferred + measure**: instrument the pytest lock, revisit with collision data in 2–4 weeks. |

## 3. Linear workflow

New lifecycle:

```
Needs Approval → Approved → In Progress → In Review → Awaiting Deploy → Done
                                                          ↘ Verify Failed (exception)
```

- **`Awaiting Deploy`** (`started`-type): where merged PRs land. Owner reconfigures the GitHub integration mapping (Linear Settings → Integrations → GitHub → FrenchForest workflow mapping): *PR merged → Awaiting Deploy*, not Done.
- **`Verify Failed`** (`started`-type): set by master only, when post-deploy verification fails. Semantics: rolled back or rollback pending; needs an owner/master decision. Never on the happy path — exception states are cheap (rare) and loud; transit states ("Deploying") were rejected as bookkeeping noise.
- **`Done`** becomes guardian-only, set after live verification, always with an evidence comment (§6).
- Multi-phase tickets keep the existing rule: stay In Progress until the final phase (unchanged).

**Owner actions (Linear UI, ~10 min):** create both states; retarget the GitHub-integration merged-PR mapping. This is step 1 of rollout — it fixes the live bug.

## 4. Dispatch — Linear-native

**Contract** (what `prime-worker` evaluates):

> A worker's NEXT = the issue that is `Approved` **and** labeled `stream:<mine>` **and** has no open "blocked by" relation, ordered by priority descending, oldest-first on ties.

Mechanics:

- **New labels:** `stream:build1`, `stream:build2`, `stream:adr`. An Approved issue with no stream label is approved-but-not-dispatched (today's "Approved, Backlog lane"). Existing Tier labels (`Tier-1:Opus` etc.) continue to carry the model choice.
- **Chains** (e.g., FRE-653→654→655) are expressed as Linear **"blocked by" relations**. Approving a chain dispatches only the unblocked head; when it completes, the next ticket becomes eligible automatically. If the Linear MCP cannot write relations, master uses the Linear GraphQL API key already in use for bulk operations.
- **Front-of-queue levers (master):** (1) priority bump — `Urgent` is reserved as the dispatch lever, not a severity opinion; (2) move the stream label between tickets.
- **No controller service.** The Linear GitHub integration handles merged→Awaiting Deploy; workers keep the existing 20-minute monitor poll (Claude Code sessions cannot receive webhooks — polling is the session-native event mechanism). The review's "delivery controller" responsibilities collapse into: Linear config (state moves), the dispatch contract above, and the existing linear-gate approval check.

**Skill/doc updates:**

- `prime-worker`: query Linear for the dispatch contract instead of parsing MASTER_PLAN's board.
- `master`: dispatch = label/priority/relation mutations; drop board editing; retain MASTER_PLAN strategy upkeep.
- `build`: unchanged except it verifies its ticket carries its own stream label.
- **MASTER_PLAN** drops the stream board and NEXT assignments; keeps strategy, wave sequencing, dependency rationale, deploy-class policy, archive pointers.

## 5. GitHub ruleset + CI

### 5.1 Ruleset "Main" (D4/C1)

Add to the existing ruleset:

- **Required status checks** (exact check names, all must pass before main updates): `Backend unit tests`, `Backend integration tests (transport)`, `Lint (mypy + ruff)`, `PWA unit tests (Vitest)`, `PWA e2e tests (Playwright)`, `Telemetry surface reconciliation (gate)`, and the **CodeQL aggregate check** (the check-run that carries alert failures — the Analyze jobs alone can read green while the aggregate is red; this exact gap produced the 15/15-green misreport).
- Keep: deletion + non-fast-forward blocks. No bypass actors (a bypass for the owner would exempt every session, since all sessions share the identity).
- **Consequence (accepted):** direct pushes to main are effectively dead — a required-checks rule blocks any ref update whose commit hasn't passed checks. Merging red becomes mechanically impossible rather than procedurally forbidden.

### 5.2 Docs flow change (consequence of C1)

Master's direct-to-main docs pushes become **auto-merge PRs**: push branch → open PR → `gh pr merge --auto --squash` → path-aware CI passes docs-only changes in ~1–2 min → lands. Fully automated inside the master skill; ~2 min added latency on the least time-sensitive artifact we produce. Worker PR flow is unchanged.

### 5.3 CI (D5)

- **Triggers:** `push: [main]` + `pull_request` — kills the duplicate run on PR branches.
- **Path-aware in-job skips** (jobs always report a conclusion so required checks never hang): PWA unit/e2e run fully only when `seshat-pwa/**` changed; backend integration only when `src/**`/`tests/**`/deps changed; docs-only changes pass all checks in ~1 min. Lint + backend unit always run. Implementation: a `changes` detection step (e.g., `dorny/paths-filter`) with early-exit-green, **not** workflow-level `paths:` filters (those leave required checks pending forever).
- **Rejected:** merge queue (merges are guardian-serialized — validates nothing extra), separate fast/domain workflow files (in-job skips achieve the same signal with less YAML), nightly regression lane (add later if flake data justifies).

## 6. Evidence comment on Done

Master closes every ticket with a fixed template (plain prose + links — no code blocks, CLI, or SQL tokens, which the Cloudflare WAF rejects on Linear writes):

> **Delivery evidence** — PR #NNN · merge SHA · CI run link · deploy class (auto / ask-first) and who authorized · deploy timestamp · health check result · rollback available yes/no · acceptance criteria: AC-1 verified (how), AC-2 verified (how), …

This formalizes the existing acceptance-criteria gate; the template lives in the master skill.

## 7. Deferred and rejected

| Review item | Verdict | Reason |
|---|---|---|
| GitHub environments + deploy approvals | **Rejected** | Deploys stay guardian-run on the VPS (D1); environments only gate Actions-driven deploys. |
| Self-hosted runner / Actions deploy | **Rejected** | New attack surface on a public repo; migration cost exceeds benefit for solo HITL deploys. |
| Merge queue | **Rejected** | Guardian-serialized merges; nothing to queue. |
| Claim leases + expiry | **Rejected** | Three known sessions, owner-present; monitor loop + idle-stream-clear convention covers dead sessions. |
| Webhook/event controller service | **Deferred — the intended next step (NOT rejected)** | Owner decision (2026-07-04): stabilize the poll-based automation first, then build the event-driven push channel. Corrected 2026-07-10 (this row previously read "Rejected (dissolved)" — a mischaracterization). Now under evaluation: MCP Channels (`claude/channel`) exists as of CC v2.1.80+ and is purpose-built for CI-result push. See `docs/research/2026-07-10-event-driven-dispatch-actuation-capability-assessment.md` + the forthcoming ADR (Build/ADR Dispatch Automation project). |
| Per-stream Compose isolation | **Deferred — measure first (D6)** | Second stack (~2.5 GiB) fits RAM but not the CPU-saturation cause of the lock; instrument `.claude/hooks/check-pytest-lock.sh` to log each blocked attempt (timestamp + stream), revisit in 2–4 weeks with the collision rate. |
| Generated dispatch board in MASTER_PLAN | **Rejected** | With dispatch in Linear there is nothing left worth generating; a stale snapshot recreates the drift problem. |
| Second GitHub identity / repo-auditor app | **Rejected for now** | Heavy for a solo research project; revisit if a real integration actor ever exists. |

## 8. Rollout

Ordered; each step independently verifiable. Steps 3–6 are ticket-sized (owner decides at spec review whether to run them as FRE tickets through the normal gate or as direct process work).

| Step | Actor | Action | Verify |
|---|---|---|---|
| 1 | Owner | Linear UI: add `Awaiting Deploy` + `Verify Failed`; retarget GitHub integration merged-PR mapping | Merge any PR → issue lands in Awaiting Deploy, not Done |
| 2 | Master | Create `stream:*` labels; label + prioritize + relate the current Approved queue | `list_issues state=Approved label=stream:build1` returns the intended NEXT first |
| 3 | Master | Update `prime-worker` / `master` / `build` skills to the dispatch contract + evidence template + auto-merge docs flow | Worker prime run resolves the same NEXT as the old board would |
| 4 | Master | Slim MASTER_PLAN to strategy-only | No per-ticket board state remains in the file |
| 5 | Build (PR) | CI: dedupe triggers + path-aware jobs | Docs-only PR: all checks green ≤ ~2 min; PWA-only PR skips backend integration |
| 6 | Owner+Master | Add required checks to ruleset "Main" (after step 5 is merged and check names confirmed) | Direct push to main is rejected; PR with red check cannot merge |
| 7 | Master | Pytest-lock telemetry line in `check-pytest-lock.sh` | Blocked attempt produces a log line with timestamp + stream |

Ordering constraint: **step 6 must follow step 5** — required checks reference exact check names, and the docs auto-merge flow depends on fast path-aware CI; enabling the ruleset first would strand docs updates behind 7-minute full runs.

## 9. Success criteria

- No issue reaches Done without a deploy-verification evidence comment (spot-check: next 5 closed tickets).
- Zero false-Done incidents from PR merges after step 1.
- A worker session primes to the correct NEXT from Linear alone, with MASTER_PLAN's board deleted.
- A deliberately red PR cannot be merged (test once after step 6).
- Docs update lands on main in ≤ 3 min via auto-merge.
- Pytest-lock collision data exists for the isolation decision review (~2026-07-25).

## 10. Revised operating principle

> Linear decides what is approved, who owns it, and what runs next.
> MASTER_PLAN decides why this sequence matters.
> GitHub proves whether the change is safe to merge — mechanically.
> The guardian decides whether and when it deploys, and proves it is live.
> Only then may Linear say Done.
