# Dev/Deploy Process v2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement `docs/superpowers/specs/2026-07-04-dev-deploy-process-v2-design.md` — kill Linear false-Done, move dispatch to Linear, harden the main ruleset with required checks, dedupe + path-filter CI, and instrument the pytest lock.

**Architecture:** Process/config change, not product code. Linear gains 2 states + `stream:*`/`context:keep` labels; the Stream Board's job moves into Linear facts (state + label + priority + blocked-by relations); five `.claude/skills` docs are rewritten to the new contract; `ci.yml` gets a `changes` job with job-level conditionals; the existing GitHub ruleset "Main" gains required status checks; the pytest-lock hook logs blocked attempts.

**Tech Stack:** Linear MCP (`claude.ai Linear` tools) + Linear GraphQL API (relations fallback; key = `AGENT_LINEAR_API_KEY` env or `.env` at git toplevel, same resolution as `scripts/reconcile_board.py:load_linear_key`), GitHub Actions (`dorny/paths-filter@v3`), GitHub rulesets API, bash hook.

## Global Constraints

- All doc/skill/plan commits go **direct to main** (docs convention) — until Task 9 lands, after which main requires PRs; Task 9 is deliberately last.
- The `ci.yml` change (Task 8) is **code**: feature branch + PR through the master gate.
- Job `name:` fields in `ci.yml` must NOT change — they become required-check identifiers in Task 9.
- Linear writes: plain prose only (no code blocks / CLI / SQL tokens — Cloudflare WAF rejects them).
- Linear MCP: team is `FrenchForest`; new issues state `Needs Approval`.
- Execution order is the spec's §8 order; **Task 9 must follow Task 8's merge** (check names + fast docs lane must exist first).
- Tasks 1 and 9 contain **owner-only actions** (Linear workspace admin / GitHub repo admin); the session prepares, requests, and verifies — it cannot perform them.

---

### Task 0: Linear tracking ticket for the rollout

**Files:** none (Linear only)

**Interfaces:**
- Produces: one FRE issue ID used in every commit message and the Task 8 PR (`fre-XXX-process-v2-ci` branch naming).

- [ ] **Step 1: Create the umbrella ticket**

Via Linear MCP `save_issue`: team `FrenchForest`, state `Needs Approval`, labels `PersonalAgent` + `Tier-1:Opus` (execution is master-session process work, not routable boilerplate). Title: `Dev/Deploy Process v2 rollout (spec 2026-07-04)`. Description (plain prose): implements the approved spec at docs/superpowers/specs/2026-07-04-dev-deploy-process-v2-design.md — two new Linear states, Linear-native dispatch labels and relations, skill updates, MASTER_PLAN slim, CI dedupe with path-aware jobs, ruleset required checks, pytest-lock telemetry. Acceptance criteria mirror spec section 9.

- [ ] **Step 2: Owner approves**

Ask the owner to move it to `Approved` (they approved the spec; this is the Linear-side formality that keeps the gate honest). Verify with `get_issue` → state `Approved` before proceeding to Task 2+.

---

### Task 1: Linear workspace changes (OWNER, in Linear UI)

**Files:** none (Linear workspace settings)

**Interfaces:**
- Produces: states `Awaiting Deploy` and `Verify Failed` (both `started`-type) on team FrenchForest; GitHub integration merged-PR mapping retargeted; repo auto-merge enabled.

- [ ] **Step 1: Owner adds the two states**

Linear → Settings → Teams → FrenchForest → Issue statuses → add under the *Started* category:
1. `Awaiting Deploy` — description: "PR merged; deploy + live verification pending (master)". Position it after `In Review`.
2. `Verify Failed` — description: "Post-deploy verification failed; rolled back or rollback pending; needs a decision". Position after `Awaiting Deploy`.

- [ ] **Step 2: Owner retargets the GitHub integration**

Linear → Settings → Integrations → GitHub → workflow/status automation for FrenchForest: set **"When PR is merged" → `Awaiting Deploy`** (currently moves to Done — the false-Done bug). Leave other mappings (branch link → In Progress etc.) as they are.

- [ ] **Step 3: Owner enables repo auto-merge (needed by Task 6/9 docs flow)**

GitHub → `alextra-lab/personal_agent` → Settings → General → check **"Allow auto-merge"**. (While here, optionally check "Automatically delete head branches" — the backstop `master` SKILL Step 5 notes the PAT can't enable.)

- [ ] **Step 4: Verify from the session**

Run Linear MCP `list_issue_statuses(team="FrenchForest")` → expect both new names present with type `started`. Auto-merge: `gh api repos/alextra-lab/personal_agent --jq .allow_auto_merge` → `true`. Integration mapping is verified live at the next real PR merge (expected: issue lands `Awaiting Deploy`, not `Done`) — note this as a pending check in the session.

---

### Task 2: Dispatch labels + encode the current queue in Linear

**Files:** none (Linear only)

**Interfaces:**
- Consumes: current Stream Board (`docs/plans/MASTER_PLAN.md` lines 13–19) as the last authoritative snapshot — re-read it at execution time; it is deleted in Task 7.
- Produces: labels `stream:build1`, `stream:build2`, `stream:adr`, `context:keep`; the Approved queue labeled + chained so the Task 4/5 query resolves the same NEXT the board shows today.

- [ ] **Step 1: Create the four labels**

Linear MCP `create_issue_label` on team FrenchForest ×4: `stream:build1`, `stream:build2`, `stream:adr` (pick three distinct colors), `context:keep` (description: "dispatch context flag — present = KEEP warm context; absent = CLEAR").

- [ ] **Step 2: Label the current queues (from the board at execution time; as of writing):**

- build1: add `stream:build1` to FRE-769, FRE-770, FRE-771, FRE-772, FRE-773, FRE-699, FRE-472 (`save_issue` label add — do not disturb existing labels).
- build2: add `stream:build2` to FRE-649.
- adr: nothing (board says owner-pick; an unlabeled Approved queue is the correct encoding).

- [ ] **Step 3: Encode the chains as blocked-by relations**

Target relations (X blocks Y): 769→770, 770→771, 771→772, 772→699, 699→472. FRE-773 is an independent gate — no relation. Try the MCP first (`save_issue` — check whether it accepts relations); if not, use GraphQL with the `AGENT_LINEAR_API_KEY` (env or `.env`, per `scripts/reconcile_board.py:load_linear_key`):

```bash
# one call per edge; get issue UUIDs first via the id query
curl -s https://api.linear.app/graphql \
  -H "Authorization: $AGENT_LINEAR_API_KEY" -H "Content-Type: application/json" \
  -d '{"query":"query{ issue(id: \"FRE-769\"){ id } }"}'
curl -s https://api.linear.app/graphql \
  -H "Authorization: $AGENT_LINEAR_API_KEY" -H "Content-Type: application/json" \
  -d '{"query":"mutation{ issueRelationCreate(input:{ issueId: \"<uuid-of-769>\", relatedIssueId: \"<uuid-of-770>\", type: blocks }){ success } }"}'
```

- [ ] **Step 4: Verify the dispatch query resolves correctly**

`list_issues(team="FrenchForest", state="Approved", label="stream:build1")` → FRE-769 must be the head after ordering by priority then createdAt, and FRE-770/771/772/699/472 must each show an open blocking relation (spot-check FRE-770 via `get_issue` — confirm the relation is visible; if MCP doesn't expose relations, verify via the GraphQL `issue { relations { nodes { type relatedIssue { identifier } } } }` query and note in the rollout ticket that workers use GraphQL for the block-check too).

---

### Task 3: `lifecycle-rules.md` — new lifecycle, dispatch contract, evidence template

**Files:**
- Modify: `/opt/seshat/.claude/skills/lifecycle-rules.md`

**Interfaces:**
- Produces: § "Dispatch (Linear-native)" and § "Evidence comment (close-out template)" — referenced by Tasks 4, 5, 6. State-lifecycle line consumed by all role skills.

- [ ] **Step 1: Replace the state-lifecycle + auto-close bullets**

Replace the two bullets at lines 58–60 (`**State lifecycle …**` and `**Auto-close trap:** …`) with:

```markdown
- **State lifecycle — the board must not lie (be accurate, no stale entries):**
  `Approved` (ready; dispatched once it also carries a `stream:*` label) → `In Progress` (a session is
  building it **now** — ≤1 per stream, transient; umbrellas/pillars go to `Backlog`, parked-project
  tickets to `Approved`, never left In Progress) → `In Review` (PR open, at master's gate) →
  `Awaiting Deploy` (merged; deploy + live verification pending) → `Done` (deploy-verified live;
  master flips it deliberately, with the evidence comment below). Exception state: `Verify Failed`
  (post-deploy verification failed — rolled back or rollback pending; set by master only; demands a
  decision, never appears on the happy path).
- **GitHub integration (retargeted 2026-07-04):** merging a `fre-XXX`-branched PR auto-moves the
  ticket to `Awaiting Deploy` — never Done. The old auto-Done trap is closed by configuration; if a
  merged ticket ever shows `Done` without an evidence comment, the integration mapping has drifted —
  fix the mapping, don't just reopen the ticket.
```

- [ ] **Step 2: Add the dispatch contract section (after `## Ticket state`, before `### Evidence contract`)**

```markdown
## Dispatch (Linear-native)

Dispatch state lives in Linear, not MASTER_PLAN (process v2, 2026-07-04). A worker's NEXT is:

> the FrenchForest issue that is **`Approved`** AND labeled **`stream:<mine>`** AND has **no open
> "blocked by" relation**, ordered by **priority** (descending; `Urgent` is master's front-of-queue
> lever, not a severity opinion), **oldest created first** on ties.

- **Model** = the ticket's `Tier-*` label. **Context** = the `context:keep` label (present → KEEP the
  warm context; absent → CLEAR, the default).
- **Master owns every dispatch mutation** — stream labels, priority, `context:keep`, blocked-by
  relations. Workers only read. An `Approved` issue with **no** stream label is
  approved-but-not-dispatched.
- **Chains** are "blocked by" relations; only the unblocked head is pickable, and completing it
  automatically exposes the next — no re-dispatch step.
- **Busy guard:** if any issue is `In Progress` with this stream's label, the stream is building —
  do not resolve a new NEXT.
```

- [ ] **Step 3: Add the evidence template (append inside `### Evidence contract (proof of Done)`)**

```markdown
**Close-out evidence comment (master, on every Done — plain prose + links, no code blocks / CLI / SQL
tokens; the WAF rejects them):** PR link · merge SHA · CI run link · deploy class (standing-approval
class or ask-first, and who authorized) · deploy timestamp · health/verification result · rollback
available yes/no · each acceptance criterion with how it was verified. A ticket reaching Done without
this comment is drift — catch it.
```

- [ ] **Step 4: Commit**

```bash
git -C /opt/seshat add .claude/skills/lifecycle-rules.md
git -C /opt/seshat commit -m "docs(process): lifecycle v2 — Awaiting Deploy/Verify Failed, Linear-native dispatch contract, evidence template (FRE-XXX)"
git -C /opt/seshat push origin main
```

---

### Task 4: `prime-worker` — resolve NEXT from Linear

**Files:**
- Modify: `/opt/seshat/.claude/skills/prime-worker/SKILL.md` (Step 4 block, lines 52–64)

**Interfaces:**
- Consumes: lifecycle-rules § Dispatch (Task 3), labels/relations (Task 2).

- [ ] **Step 1: Replace Step 4 (lines 52–64) with:**

```markdown
## Step 4 — Resolve NEXT from Linear (the dispatch authority; uniform for build & adr)
Dispatch contract = lifecycle-rules § Dispatch (Linear-native). Resolve in two queries:

1. **Busy guard:** `list_issues(team="FrenchForest", state="In Progress", label="stream:<mine>")` —
   any result → a session is on it → **silent.** (This replaces the old board-based
   "already dispatched" check and is uniform across build & adr branch-naming schemes.)
2. **Head of queue:** `list_issues(team="FrenchForest", state="Approved", label="stream:<mine>")` —
   order by priority (Urgent first), oldest created on ties; walk from the top and take the first
   issue with **no open "blocked by" relation** (check via `get_issue`; if relations aren't exposed
   there, the Linear GraphQL relations query is the fallback — key per
   `scripts/reconcile_board.py:load_linear_key`). Blocked = blocking issue not Done/Canceled.
- No candidate → **silent** (nothing dispatched to this stream — master hasn't labeled work for it).
- Candidate found → read its **Tier label** ([O]/[S]/[H] → Opus/Sonnet/Haiku) and its **context
  flag** (`context:keep` label present → KEEP; absent → CLEAR) → **advise** (Step 5).
```

Note: the old Step-4 states branch (`Approved`/`In Progress`/`Needs Approval`/`Done`) collapses into the two queries — the query itself filters to Approved, and the busy guard covers In Progress; keep Step 5 (dispatch card) unchanged apart from wording that references "the board's model tag" → "the ticket's Tier label".

- [ ] **Step 2: Sweep residual board references in this file**

Line 10 "board + Linear state" → "Linear state"; line 77–78 "Master owns the board, so you only ever advise the ticket master assigned" → "Master owns dispatch (labels/priority/relations), so you only ever advise the ticket master routed to this stream".

- [ ] **Step 3: Commit** (same pattern as Task 3 Step 4, message `docs(process): prime-worker resolves NEXT from Linear dispatch contract (FRE-XXX)`)

---

### Task 5: `build` + `adr` — resolve from Linear; fix stale integration language

**Files:**
- Modify: `/opt/seshat/.claude/skills/build/SKILL.md` (lines 8–14, 31–34)
- Modify: `/opt/seshat/.claude/skills/adr/SKILL.md` (lines 11–22, 39–48)

- [ ] **Step 1: `build` — replace the stream-selector paragraph (line 10) with:**

```markdown
**Stream selector (`1`/`2`) → resolve NEXT from Linear** (dispatch contract: lifecycle-rules
§ Dispatch). **FIRST `git fetch origin`** (you still need latest main for Step 0). Then: busy guard —
`list_issues(state="In Progress", label="stream:build<N>")` non-empty → STOP (a session is already on
it); otherwise take the head of `list_issues(state="Approved", label="stream:build<N>")` ordered by
priority then oldest-created, skipping any issue with an open "blocked by" relation. That issue is the
ticket to build. Honor its **context flag** (`context:keep` label → KEEP; absent → CLEAR):
```

(The CLEAR/KEEP bullets that follow stay unchanged; line 14's "If the board row is missing/ambiguous" → "If the queue is empty or ambiguous, STOP and ask master.")

- [ ] **Step 2: `build` — fix the integration sentence (lines 32–34)**

Replace "Linear is disconnected from GitHub (2026-06-26) — nothing auto-moves status anymore, so the session doing the work owns the In Progress transition; master owns the Done transition at the gate." with:

```markdown
The GitHub integration only automates the merge transition (PR merged → `Awaiting Deploy`,
retargeted 2026-07-04); the session doing the work owns the In Progress transition; master owns the
Done transition at the gate.
```

- [ ] **Step 3: `adr` — same two changes**

Lines 11–22: replace the board-resolution paragraph with the Linear resolution (identical mechanics, label `stream:adr`, dispatch command `/adr`; keep the blank-session CLEAR/KEEP text and "explicit FRE-… id overrides"). Lines 47–48: replace "Linear is disconnected from GitHub (2026-06-26), so status never moves automatically; …" with the same corrected sentence as Step 2.

- [ ] **Step 4: Commit** (`docs(process): build/adr resolve NEXT from Linear; fix stale GitHub-integration language (FRE-XXX)`)

---

### Task 6: `master` + `prime-master` — close-out, dispatch mutations, docs auto-merge flow

**Files:**
- Modify: `/opt/seshat/.claude/skills/master/SKILL.md` (Steps 5, 8; add Step 9)
- Modify: `/opt/seshat/.claude/skills/prime-master/SKILL.md` (line 23)

- [ ] **Step 1: `master` Step 5 (Merge) — append:**

```markdown
On merge the Linear integration auto-moves the ticket to **`Awaiting Deploy`** (never Done) — confirm
it did; if it landed anywhere else the integration mapping has drifted (lifecycle-rules § Ticket state).
```

- [ ] **Step 2: `master` Step 8 (Close out) — replace the three bullets with:**

```markdown
- Update MASTER_PLAN on `main` if strategy/sequencing changed (bump "Last updated"). **Docs-to-main
  flow (required checks are active on main):** `git switch -c docs/<slug>` → commit → push →
  `gh pr create` → `gh pr merge --auto --squash` — path-aware CI passes docs-only changes in ~1–2 min
  and the PR lands itself; then `git switch main && git pull`.
- **Close the ticket: state → Done + the evidence comment** (template: lifecycle-rules § Evidence
  contract — plain prose + links; PR, merge SHA, CI run, deploy class + authorization, deploy
  timestamp, verification result, rollback availability, each acceptance criterion + how verified).
- **If verification failed: state → `Verify Failed`** (not Done, not left in Awaiting Deploy), file
  the follow-up issue, consider rollback. Verify Failed is the exception flag that demands a decision.
- **Advance dispatch (replaces advancing the board):** confirm the completed chain exposed the right
  next head (its blocked-by cleared); if the stream's queue is empty or priorities changed, apply the
  mutations — `stream:*` label, priority (Urgent = front-of-queue lever), `context:keep` per the build
  session's context-disposition comment, blocked-by relations for any new chain.
```

- [ ] **Step 3: `prime-master` line 23 — update the Linear snapshot query**

Replace "Linear: list In Progress + Pending Verification tickets on FrenchForest." with:

```markdown
4. Linear: list `In Progress` + `Awaiting Deploy` + `Verify Failed` tickets on FrenchForest —
   Awaiting Deploy = merged-not-verified backlog (master's queue); Verify Failed = open exceptions.
```

Also line 15 "MASTER_PLAN ↔ Linear in sync" stays — it now means strategy narrative only.

- [ ] **Step 4: Commit** (`docs(process): master close-out v2 — Awaiting Deploy confirm, evidence comment, Verify Failed, dispatch mutations, docs auto-merge flow (FRE-XXX)`)

---

### Task 7: Slim MASTER_PLAN — strategy only

**Files:**
- Modify: `/opt/seshat/docs/plans/MASTER_PLAN.md` (delete lines 9–24: `## 🎛️ Stream Board` section incl. markers + context-flag para; edit lines 101–106 "How This File Works")

**Interfaces:**
- Consumes: Task 2 (queue must already be encoded in Linear) and Tasks 4–6 (no consumer parses the board anymore). **Do not run before Tasks 2–6 are done.**

- [ ] **Step 1: Replace the Stream Board section (lines 9–24) with:**

```markdown
## Dispatch

Dispatch lives in **Linear** (process v2, 2026-07-04 — contract in `.claude/skills/lifecycle-rules.md`
§ Dispatch): a stream's NEXT = `Approved` + `stream:<name>` label + no open blocked-by relation,
priority-ordered. This file carries **why the sequence is what it is** — priorities, waves,
dependency rationale — never per-ticket board state (it drifts).

**Parked / held (not dispatched — rationale only):** ADR-0102 vision-doc chain FRE-682–689
(un-paused 2026-07-04, LOW — sequenced behind Memory) · Inference ADR-0094/0095 trees FRE-600–604 /
607–611 (held pending FRE-432/516 measurement) · FRE-713 (trigger-gated, Backlog).
```

- [ ] **Step 2: Update "How This File Works" (lines 101–106)**

Replace the "**Dispatch** = the 🎛️ Stream Board…" bullet with: `**Dispatch** = Linear (state + stream label + priority + relations; lifecycle-rules § Dispatch). Priority order = the numbered list above. Cross-project sequencing = [sessions/2026-07-02-priority-sequencing.md](sessions/2026-07-02-priority-sequencing.md).` and in the "Update after every ship" bullet change "advance the Stream Board + bump" → "apply any dispatch mutations in Linear + bump".

- [ ] **Step 3: Bump the header "Last updated" line** (note: "process v2 — dispatch moved to Linear; Stream Board removed").

- [ ] **Step 4: Verify nothing still parses the board**

```bash
grep -rn "STREAM-BOARD\|Stream Board" /opt/seshat/.claude /opt/seshat/scripts /opt/seshat/docs/plans/MASTER_PLAN.md
```
Expected: no hits outside archived/completed docs. Then `uv run python scripts/reconcile_board.py` → still runs (it parses only the header narrative + Linear; confirm no traceback).

- [ ] **Step 5: Commit** (`docs(master): MASTER_PLAN → strategy-only; dispatch moved to Linear (FRE-XXX)`)

---

### Task 8: CI — dedupe triggers + path-aware jobs (BRANCH + PR)

**Files:**
- Modify: `/opt/seshat/.github/workflows/ci.yml` (header comment, `on:` block, add `changes` job, add `needs`/`if` to 4 jobs)

**Interfaces:**
- Produces: unchanged check names (`Backend unit tests`, `Backend integration tests (transport)`, `Lint (mypy + ruff)`, `Telemetry surface reconciliation (gate)`, `PWA unit tests (Vitest)`, `PWA e2e tests (Playwright)`) + new non-required check `Detect changed paths` — consumed by Task 9's required-checks list.

- [ ] **Step 1: Branch**

```bash
git -C /opt/seshat switch -c fre-XXX-process-v2-ci origin/main
```

- [ ] **Step 2: Edit `on:` block (lines 20–24) to:**

```yaml
on:
  push:
    branches: [main]
  pull_request:
    branches: ["**"]
```

Also update the header comment lines 2–3 to: `# Runs on pull_request (all branches) and push to main only — PR branches are validated once, not twice (process v2, 2026-07-04). Jobs are path-aware: job-level if-skips report a skipped conclusion, which satisfies required status checks.`

- [ ] **Step 3: Insert the `changes` job as the first job (after `jobs:` line 29):**

```yaml
  # ── Changed-path detection (drives job-level skips; NOT a required check) ────
  changes:
    name: Detect changed paths
    runs-on: ubuntu-24.04
    outputs:
      backend: ${{ steps.filter.outputs.backend }}
      pwa: ${{ steps.filter.outputs.pwa }}
    steps:
      - uses: actions/checkout@v4
      - uses: dorny/paths-filter@v3
        id: filter
        with:
          filters: |
            backend:
              - 'src/**'
              - 'tests/**'
              - 'scripts/**'
              - 'docker/**'
              - 'config/**'
              - 'pyproject.toml'
              - 'uv.lock'
              - '.github/workflows/ci.yml'
            pwa:
              - 'seshat-pwa/**'
              - '.github/workflows/ci.yml'
```

- [ ] **Step 4: Gate the four expensive jobs**

Add to `backend-integration` and `telemetry-surface`:

```yaml
    needs: changes
    if: github.event_name == 'push' || needs.changes.outputs.backend == 'true'
```

Add to `pwa-unit` and `pwa-e2e`:

```yaml
    needs: changes
    if: github.event_name == 'push' || needs.changes.outputs.pwa == 'true'
```

`backend-unit` and `lint` stay unconditional (always-on fast signal; spec §5.3). On `push` to main everything runs unconditionally (post-merge full validation).

- [ ] **Step 5: Validate + PR**

```bash
actionlint /opt/seshat/.github/workflows/ci.yml 2>/dev/null || uv tool run --from actionlint-py actionlint /opt/seshat/.github/workflows/ci.yml
git -C /opt/seshat add .github/workflows/ci.yml && git -C /opt/seshat commit -m "ci: dedupe triggers (push=main only) + path-aware job skips (FRE-XXX)"
git -C /opt/seshat push -u origin fre-XXX-process-v2-ci
gh pr create --title "CI: dedupe triggers + path-aware jobs (FRE-XXX)" --body "Implements spec 2026-07-04 §5.3 (D5). Check names unchanged (required-checks prerequisite for the ruleset task)."
```

- [ ] **Step 6: Verify on the PR itself, then merge via master gate**

Expected on this PR (touches `.github/workflows/ci.yml` → both filters true): all jobs run once (no duplicate push-triggered run alongside). After merge, open a trivial docs-only test PR (e.g. one-line README touch): expected — `backend-integration`, `telemetry-surface`, `pwa-unit`, `pwa-e2e` show **skipped**, `backend-unit` + `lint` green, total wall-clock ≤ ~4 min. Close the test PR after checking, or keep it for Task 9 Step 3.

---

### Task 9: Ruleset "Main" — required status checks (OWNER-assisted; AFTER Task 8 merges)

**Files:** none (GitHub ruleset 18221121)

**Interfaces:**
- Consumes: Task 8 merged (check names live under the new trigger scheme); Task 6 (master skill already documents the docs auto-merge flow); Task 1 Step 3 (auto-merge enabled).

- [ ] **Step 1: Confirm the exact CodeQL aggregate check name**

On any recent PR: `gh pr checks <PR#>` and `gh api repos/alextra-lab/personal_agent/commits/<head-sha>/check-runs --jq '.check_runs[].name'` — the aggregate code-scanning check is expected to be named `CodeQL`; use whatever exact name appears (NOT the three `Analyze (...)` jobs — memory `reference_codeql_aggregate_check_vs_analyze_jobs`: alerts live on the aggregate, Analyze jobs can be green while it's red).

- [ ] **Step 2: Update the ruleset**

Try via API first (may 403 — the PAT lacks admin):

```bash
gh api -X PUT repos/alextra-lab/personal_agent/rulesets/18221121 --input - <<'EOF'
{
  "name": "Main",
  "target": "branch",
  "enforcement": "active",
  "conditions": { "ref_name": { "include": ["refs/heads/main"], "exclude": [] } },
  "rules": [
    { "type": "deletion" },
    { "type": "non_fast_forward" },
    { "type": "required_status_checks",
      "parameters": {
        "strict_required_status_checks_policy": false,
        "required_status_checks": [
          { "context": "Backend unit tests" },
          { "context": "Backend integration tests (transport)" },
          { "context": "Lint (mypy + ruff)" },
          { "context": "Telemetry surface reconciliation (gate)" },
          { "context": "PWA unit tests (Vitest)" },
          { "context": "PWA e2e tests (Playwright)" },
          { "context": "CodeQL" }
        ]
      }
    }
  ],
  "bypass_actors": []
}
EOF
```

If 403: give the owner the equivalent UI path — GitHub → repo → Settings → Rules → Rulesets → Main → add rule "Require status checks to pass" → add the seven checks above verbatim → Save. No bypass actors (a bypass for the owner account would exempt every session).

- [ ] **Step 3: Verify both enforcement directions**

1. Red-blocked: on the docs-only test PR from Task 8 Step 6 (or a new scratch PR), confirm the merge button/API refuses while any required check is pending — `gh pr merge <n> --squash` → expected: refusal citing required status checks (don't force a red check; pending proves the wiring).
2. Direct-push-blocked: from a scratch clone state, `git commit --allow-empty -m "ruleset probe" && git push origin main` → expected: **rejected** with a rulesets violation. Then delete the local probe commit (`git reset --hard origin/main`).
3. Docs flow end-to-end: make a one-line docs change via the Task 6 flow (branch → PR → `gh pr merge --auto --squash`) → expected: lands on main without manual intervention in ≤ ~4 min (spec §9 says ≤3 min target; record actual).

- [ ] **Step 4: Update lifecycle-rules + root docs of the new invariant**

Append one line to `lifecycle-rules.md` § MASTER_PLAN: `- Main requires green checks on every update (ruleset, 2026-07-04) — all commits to main land via PR (docs use the auto-merge flow in /master Step 8).` Commit via the new docs flow itself (this is its first real use).

---

### Task 10: Pytest-lock telemetry

**Files:**
- Modify: `/opt/seshat/.claude/hooks/check-pytest-lock.sh` (inside the `if pgrep …` block, after line 21)

- [ ] **Step 1: Add the log line**

Insert between line 21 (`pids=$(…)`) and line 22 (`printf "BLOCKED…`):

```bash
        # FRE-XXX: collision telemetry for the substrate-isolation decision (spec §7 D6).
        log_dir="$(dirname "$(git rev-parse --path-format=absolute --git-common-dir 2>/dev/null)")/telemetry"
        mkdir -p "$log_dir" 2>/dev/null && \
            echo "$(date -u +%FT%TZ) worktree=$(basename "$(git rev-parse --show-toplevel 2>/dev/null)") blocked_pids=${pids% }" \
            >> "$log_dir/pytest_lock_blocks.log"
```

(`--git-common-dir` resolves to the primary repo's `.git` from any worktree, so all streams append to the single gitignored `/opt/seshat/telemetry/` file without a hardcoded path.)

- [ ] **Step 2: Verify by simulation**

```bash
python3 -m pytest --collect-only -q -k zzz_nonexistent tests/ >/dev/null 2>&1 &
sleep 1
echo '{"tool_input":{"command":"uv run python -m pytest tests/foo.py"}}' | /opt/seshat/.claude/hooks/check-pytest-lock.sh; echo "exit=$?"
wait
tail -1 /opt/seshat/telemetry/pytest_lock_blocks.log
```

Expected: `BLOCKED: pytest already running…`, `exit=2`, and a fresh log line with UTC timestamp + `worktree=seshat`. Confirm `telemetry/` is gitignored (`git check-ignore telemetry/pytest_lock_blocks.log` → prints the path).

- [ ] **Step 3: Commit + set the review reminder**

Commit via the docs auto-merge flow (`chore(hooks): log pytest-lock collisions for the isolation decision (FRE-XXX)`). Add to the rollout ticket a comment: review the collision log ~2026-07-25 (spec §9).

---

## Final close-out

- [ ] All spec §9 success criteria checked and evidenced on the rollout ticket (evidence comment format from Task 3).
- [ ] Rollout ticket → Done by master with the evidence comment — the first ticket closed under the process it ships.
