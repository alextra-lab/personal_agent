# Architecture Review → Ticket & ADR Plan (for the master session)

> **Author:** adr session (review-only; does not create tickets, modify src/, or merge).
> **Audience:** master session (creates Linear tickets) + adr session (authors the ADRs).
> **Date:** 2026-06-26
> **Provenance of the source:** a design-review conversation with Alex, handed in via `/remote-control`. The source doc's claims were marked `[CONFIRMED]` / `[VERIFY]` / `[JUDGMENT]`. **Every `[VERIFY]` claim below has now been checked against the live tree at `/opt/seshat/.claude/worktrees/adrs`.** Where reality diverged, the code wins and the divergence is noted.

---

## Context — why this plan exists

Alex held an external design-review of Seshat and produced a 16-item action plan (DOC-1…4, DB-1, RT-1/2, EVAL-1/2/3, ARCH-1/2/3, GOV-1/2, REF-1/2). The plan was written **without codebase access**, so its job here was triage: verify each claim, then convert the survivors into properly-homed Linear tickets and ADRs without disrupting the owner's standing priority (**finish infrastructure + observability first**).

**Owner decisions taken during this review (2026-06-26):**
1. **Homing:** distribute items into existing L0–L3 pillars; create a new project only if an item is *sufficiently tangential* to all of them. Infra/observability remains the build priority. **Bugs are placed by criticality, not parked behind the streams.**
2. **Design-heavy items go ADR-first** — routed to the adr session (ADR-0094+), implementation tickets filed only after each ADR is approved.
3. **Residual measurement/reflection items** distribute into existing pillars unless tangential.

**Net outcome of verification:** 1 real bug (DB-1 `sessions.user_id`), 2 confirmed doc-drift items, 1 schema-debt audit, 3 ADR-worthy design forks, and several measurement/reflection items — *minus* a handful of source-doc claims that the code refuted (see §5).

---

## 1. Verification summary (claim → verdict)

| Item | Source tag | Verdict vs code | Headline evidence |
|------|-----------|-----------------|-------------------|
| **DOC-1** MLX/Apple framing | CONFIRMED | **Confirmed (top docs)** — but code + ADR-0044/0045 are already backend-agnostic; VPS runs llama.cpp/GGUF | `README.md:5,93,105,413`; `CLAUDE.md:123`; contrast `llm_client/client.py:1-5,265`, `ADR-0045:304-305` |
| **DOC-2** Single-brain/Qwen3.5 | CONFIRMED | **Confirmed README stale** — reality is Qwen3.6 + sub-agent + cloud Sonnet 4.6. Live spec already qualifies "single brain" to *user-facing path only* | `README.md:34,76` vs `COGNITIVE_ARCHITECTURE_REDESIGN_v2.md:72,576-601`; `config/models.yaml:51,70,127` |
| **DOC-3** Doc↔tree drift pass | VERIFY | **Confirmed needed** — DOC-1/2 are symptoms; `CONTEXT_INTELLIGENCE_SPEC.md:133` self-flags a stale guide | as above |
| **DOC-4** "SLM Server" misnomer | JUDGMENT | **Valid decision to make** — `slm_server` is a separate repo; in-repo class is `LocalLLMClient` | `README.md:106,131`; `llm_client/client.py` |
| **DB-1** Postgres schema audit | CONFIRMED need | **Confirmed + found a real BUG** (see §2) | `service/models.py:199-204` vs `docker/postgres/init.sql:8-21` |
| **RT-1** Encode local/cloud routing | CONFIRMED policy | **Confirmed gap** — `_determine_initial_model_role` always returns PRIMARY; local/cloud is a manual per-session `execution_profile` flag; gateway has the seam | `orchestrator/executor.py:1309-1322,2234-2250`; `service/app.py:1830-1874`; `llm_client/factory.py:56-112` |
| **RT-2** Local-first→escalate | JUDGMENT | **Open research** — `allow_cloud_escalation` flag exists but no content-driven trigger | `config/profile.py:103-121`; `config/profiles/cloud.yaml` |
| **EVAL-1** Ablatable subsystems | JUDGMENT | **Partly true** — many flags exist; sub-agent/expansion has *no* clean on/off | `config/settings.py:269,1151,1188,1312`; gap: no `sub_agent_enabled` |
| **EVAL-2** Context-occupancy split | JUDGMENT | **Confirmed gap** — only a scalar total + `has_memory`/`has_tools` booleans emitted; no memory/tool/reasoning breakdown | `request_gateway/budget.py:41-65,270-283` |
| **EVAL-3** Brain A/B (local vs cloud) | JUDGMENT | **Already enabled** — `execution_profile` + dual-instance eval stack exist; this is "run the controlled eval" | `docker-compose.eval.yml`; `scripts/eval/fre453_canonical_evalset/` |
| **ARCH-1** Delegation via tool boundary + grammar | JUDGMENT | **Mixed** — context-firewall + deterministic gateway decision already TRUE; **grammar-constrained decoding is NOT wired** (only `response_format`, one `json_object` caller) | `orchestrator/sub_agent.py:5-9,322-333`; `decomposition.py:1-8`; grammar absent (grep) |
| **ARCH-2** Size sub-agent to tool messiness | JUDGMENT | **Open policy** — discovery allowlist exists; overlaps FRE-492/495 | `sub_agent.py:97-99,508` |
| **ARCH-3** Memory active vs passive | JUDGMENT | **Pick-the-default, not greenfield** — passive injection AND memory-as-tool both already exist | `request_gateway/context.py:1-10`; `tools/memory_search.py`, `tools/personal_history.py` |
| **GOV-1** Keep CL→Linear human gate | JUDGMENT | **Confirmed already structural** — no self-action path; `AWAITING_APPROVAL` → human label | `captains_log/promotion.py:1-9`, `linear_client.py:33-39` |
| **GOV-2** Slice-3 trace-sufficiency bar | JUDGMENT | **Valid** — "Slice 3" is the not-yet-built self-improvement phase (schema/stubs only) | `ADR-0041:51`, `ADR-0033:190,356` |
| **REF-1** Bio-metaphor residue | JUDGMENT | **Opportunistic note only** (source says "none required up front") | — |
| **REF-2** "Scale of one" tagging | JUDGMENT | **Opportunistic note only** | — |

---

## 2. THE BUG — DB-1a: `sessions.user_id` ORM↔SQL divergence

This is the single most actionable finding and the only true defect. **Place by criticality (High), not behind the streams.**

**What's wrong.** `SessionModel.user_id` is declared `ForeignKey("users.user_id"), nullable=False, index=True` (`service/models.py:199-204`) and `SessionRepository.create(... user_id ...)` inserts it (`session_repository.py:39-79`). But **no `user_id` column is created in any Postgres SQL** — not in `init.sql` (`CREATE TABLE sessions`, lines 8-21) and not in any migration (`0004`/`0007` `ALTER TABLE sessions` add only `primary_model_at_creation`/`model_config_path`/`execution_profile`). Verified by grep: zero `user_id` hits under `docker/postgres/` for sessions.

**Why prod still works but fresh environments break.** `database.py:25` runs `Base.metadata.create_all` at startup. `create_all` only creates **missing tables** — it never adds a column to a table that already exists. On an empty volume the docker entrypoint runs `init.sql` first (creates `sessions` *without* `user_id`), so `create_all` sees the table and skips it. A fresh `make up` / `make test-infra-up` / DR rebuild therefore yields a `sessions` table with no `user_id`, and the **first session INSERT fails** (`column "user_id" does not exist`). Existing prod survives only because its volume predates the divergence (consistent with the recent VPS-reboot recovery that reused volumes).

**Severity: High, not Urgent.** No live outage; breaks fresh provisioning, the FRE-375 test substrate, and disaster recovery.

**Before fixing — confirm the live state** (build session, read-only):
```bash
docker exec cloud-sim-postgres psql -U agent -d personal_agent -c '\d sessions'
```
If prod already has `user_id` → this is pure drift (fix init.sql to match reality). If prod lacks it → prod is one fresh-session-on-a-rebuilt-volume away from breaking; fix is the same.

**Fix shape** (no Alembic — per `project_no_alembic`): add `user_id UUID NOT NULL REFERENCES users(user_id)` + index to `init.sql`'s `sessions` table **and** a new ordered, idempotent `docker/postgres/migrations/0011_sessions_user_id.sql` that `ALTER TABLE sessions ADD COLUMN IF NOT EXISTS user_id ...` (backfill strategy for any column-less existing rows TBD in the ticket — likely the single known user). Mirror the DDL in both files per the project's init-vs-migration sync convention.

---

## 3. What the master session should file now (tickets)

All issues: state **"Needs Approval"**, label **"PersonalAgent"** (state only, no "Needs Approval" label), team **FrenchForest**, one tier label each. Owner owns the Approved gate. Model tag per `MODEL_ROUTING_POLICY` shown as [O]/[S]/[H].

### Group A — Bug (file at criticality, ahead of the streams)

**A1. `[BUG]` Fix `sessions.user_id` schema divergence (fresh-env provisioning broken)** — **High**, **[S]**
- Project: **no clean pillar → standalone** (substrate/infra hygiene; not tangential enough to warrant a new project). Tag in description: "DB-1a, architecture review 2026-06-26".
- Scope/fix: as §2. Acceptance: (a) `docker exec … \d sessions` shows `user_id` after a fresh `make test-infra-up`; (b) a fresh stack can create a session end-to-end; (c) `init.sql` and `migrations/0011_*.sql` mirror each other; (d) regression note in the test substrate.
- Verify live state first (read-only) before writing the migration.

### Group B — Documentation reconciliation (cheap, high-clarity; docs → direct-to-main per `feedback_branch_pr_for_code`)

**B1. `[DOCS]` Reconcile inference + brain-architecture docs with the live tree** — **Medium**, **[H]** (escalate to [S] if the DOC-3 sweep surfaces code-comment edits)
- Project: **standalone docs hygiene** (no pillar). Covers DOC-1 + DOC-2 + DOC-3 as one batched docs PR/commit.
- DOC-1: drop MLX/Apple-Silicon-as-required framing; reframe local inference as "backend-agnostic wrapper over an OpenAI-compatible endpoint (`LLM_BASE_URL`); current backend llama.cpp/GGUF; swappable." Touch `README.md:5,93,105,413`, `CLAUDE.md:123`, `.claude/CLAUDE.md:16`, `tools/TOOLS_OVERVIEW.md:30,122`. Leave historical ADRs (0016/0080) as-written but ensure they read as dated/superseded.
- DOC-2: correct README `Qwen3.5→Qwen3.6`, replace the "single-brain / sole reasoning center" headline with the real hybrid (reasoning brain + sub-agent + cloud Sonnet 4.6), **preserving** the correct "no router SLM / deterministic gateway" claim. Touch `README.md:34,76`. Align with `COGNITIVE_ARCHITECTURE_REDESIGN_v2.md:72,576-601` (which is already correct). **Do not** introduce the term "context firewall" — it's the reviewer's phrasing, not a repo term; describe the sub-agent as the agent-as-tool digest-return worker it is.
- DOC-3: one systematic README/specs↔`src/` drift sweep; output a checklist, fix in the same batched PR. Pick up `CONTEXT_INTELLIGENCE_SPEC.md:133` (stale `SLM_SERVER_INTEGRATION.md`).
- Acceptance: no doc/comment implies MLX is required; no "single-brain/Qwen3.5" outside historical ADRs; drift checklist attached.

### Group C — Observability measurement items (fit the *active* L0 stream → file into Observability Foundation; sequence by owner)

**C1. `[TICKET]` Emit per-request context-window occupancy breakdown (memory / tool-output / reasoning)** — **Medium**, **[S]**
- Project: **Observability Foundation** (L0; ADR-0090 telemetry surface + ADR-0092 lineage). This is *on-theme with the current Lane work* — owner may choose to interleave rather than queue.
- Scope: `budget.py` already computes the total (`_total_context_tokens`, lines 41-65) and emits only a scalar + `has_memory`/`has_tools` (lines 270-283). Add a structured per-category token split emitted to ES; add a Kibana view. **Heed `feedback_es_mappings_first_pass`** — walk every new field through the index-template dynamic_templates (floats/ratios → explicit `double`; long fields → keyword `ignore_above` trap) before writing docs.
- Acceptance: ES doc carries `{memory_tokens, tool_tokens, reasoning_tokens, total}` per turn; `_field_caps` verified; dashboard shows composition over time. This is the empirical trigger feeding the ARCH-3 ADR.

**C2. `[TICKET]` Uniform per-subsystem ablation flags for the eval loop** — **Medium**, **[S]**
- Project: **Observability Foundation** (L0; serves EVAL-1, enables EVAL-3/ARCH ablations).
- Scope: flags already exist for mode-controller/insights/promotion/proactive-memory/compression/decomposition (`config/settings.py`). The gap is **sub-agent/expansion has no clean boolean** (only tuning params + runtime governance). Add a master `sub_agent_enabled` / `expansion_enabled` toggle; document the existing flag set as the ablation registry; wire the dual-instance eval harness (`docker-compose.eval.yml`) to run a fixed task set with each subsystem on/off and report the outcome-quality delta.
- Acceptance: each listed subsystem has a documented on/off; a sample ablation run produces a per-subsystem task-success delta.

### Group D — Self-improvement governance (Wave F / Captain's Log)

**D1. `[REFLECTION/DOCS]` Document the Captain's-Log human-approval invariant (GOV-1)** — **Low**, **[H]**
- Project: **Wave F self-improvement** (existing). The code already enforces it (no self-action path; `AWAITING_APPROVAL` → human label). This item is to *write the invariant down* so it can't be eroded: a short ADR addendum or `docs/reference/` note stating self-modification requires human approval, and that any future ticket-actioning automation is itself a separately-reviewed, gated decision.
- Acceptance: a committed, explicit invariant statement linked from the Captain's-Log docs.

**D2. `[REFLECTION]` Define a measurable "trace-sufficiency" bar before Slice 3 (GOV-2)** — **Low**, **[S]**
- Project: **Wave F self-improvement** (existing; relates to the standing CL promotion gate).
- Scope: write a measurable bar (trace volume + task diversity + outcome/label coverage) that must be met before Slice-3 insights/promotion machinery is wired; add a way to track progress toward it. "Slice 3" today is schema/stub-level (`ADR-0033:190,356`) — this gate guards turning it on.
- Acceptance: a documented, quantified bar + a progress indicator.

### Group E — Opportunistic reflection notes (no tickets; capture in the plan)

- **REF-1** (bio-metaphor residue) and **REF-2** ("scale of one" load-bearing vs research tagging): the source itself says no up-front work. **Do not file tickets.** Capture as a running note that feeds **C2/EVAL-1 ablation priorities** (a subsystem that fails ablation *and* exists mainly for metaphor reasons is a prune candidate). Memory is explicitly exempt from REF-1.

---

## 4. ADR-first design items (route to the adr session — ADR-0094+)

Per owner decision, these architecture forks are **ADR-first**. The adr session is currently **free** (MASTER_PLAN line 143), so it can author these *in parallel* with Lanes A/B continuing observability — design work, no deploy, fully respects "infra/observability first." Implementation tickets are filed only after each ADR is Approved. Next free number is **ADR-0094**.

**ADR-0094 — Deterministic local/cloud execution-profile routing (RT-1 + RT-2)**
- Pillar: **Seshat Inference Architecture** (extends ADR-0082 tier-routing; sibling to FRE-432).
- Problem: routing is manual today — `_determine_initial_model_role` is a no-op (always PRIMARY), and local↔cloud is the user-toggled per-session `execution_profile` flag (`service/app.py:1830-1874`, `factory.py:56-112`). The gateway already computes intent/complexity/decomposition (`request_gateway/pipeline.py`) — a clean seam exists at `executor.py:2234-2250` and/or profile resolution.
- Decide: (a) encode Alex's by-feel policy (judgment/detail/synthesis → cloud; bulk/mechanical/grep/extract → local) as a deterministic gateway rule, keeping the manual override; (b) whether to log the local/cloud decision into the `route_traces` ledger + `agent-topology-*` projection (it currently records `model_role` but not `execution_profile`); (c) **RT-2**: whether to add a local-first→detect-insufficiency→escalate path for the ambiguous middle band (the `allow_cloud_escalation` flag exists but has no trigger). The real win is *measurability*, not automation.
- Note: **EVAL-3** (brain-vs-architecture A/B) is the validation for this ADR — list it as a follow-up eval ticket gated on ADR-0094 (harness already exists, so it's "run it," Seshat Inference Architecture, [S]).

**ADR-0095 — Delegation boundary hardening: grammar-constrained sub-agent output + sizing (ARCH-1 + ARCH-2)**
- Pillar: **Seshat Inference Architecture** (overlaps FRE-502 planner reliability, FRE-492 HITL allow-gate, FRE-495 sub-agent context length — reconcile, don't duplicate).
- Problem: the agent-as-tool context firewall and the deterministic decompose/delegate decision are already correct (`sub_agent.py:5-9`, `decomposition.py:1-8`). The gap is **shape guarantees**: no GBNF/JSON-schema constrained decoding is wired into local calls — only OpenAI `response_format`, used by a single `json_object` caller; tool-call parsing is permissive post-hoc JSON with silent drops (`sub_agent.py:417-466,596-598`). On llama.cpp the `qwen3_coder` native parser is unavailable, so grammar must carry the load.
- Decide: (a) wire grammar/schema-constrained decoding for the local server so sub-agent output shape is guaranteed; (b) **ARCH-2** per-tool(-class) sub-agent sizing — small model + grammar for mechanical tools (the existing `_DISCOVERY_TOOL_ALLOWLIST` bash/read/web_search/recall), capable model for messy tools — explicitly noting that grammar fixes *shape* but not *salience* (a too-weak distiller returns clean-but-lossy summaries). Driven by *which tools the sub-agent fronts first*.
- Acceptance of ADR: a design note + (gated) impl ticket where the reasoning brain cannot bypass the sub-agent for the delegated artifact type.

**ADR-0096 — Memory access model: active retrieval-as-tool vs passive gateway injection (ARCH-3)**
- Pillar: **Memory Recall Quality** (ADR-0087) / **ADR-0081 Extended** — coordinate, this is a default-selection decision, not greenfield.
- Problem: both paths already exist — passive Stage-6 injection (`request_gateway/context.py`) AND memory-as-tool (`tools/memory_search.py`, `tools/personal_history.py`). As memory types richen, injection competes with tool output for budget (the C1/EVAL-2 breakdown is the decision input).
- Decide: active / passive / hybrid default, explicitly noting the **shared-primitive unification** (memory-retrieval-as-tool and work-as-sub-agent-tool are the same clean tool boundary the gateway invokes) and leaning into **consolidation quality over raw storage** (the episodic→semantic pipeline in `memory/promote.py` + `second_brain/consolidator.py` is the vehicle). Decide against C1/EVAL-2 context-pressure data.

**DOC-4 — "SLM Server" rename decision** → fold into the adr session as a lightweight decision note (not a full ADR): rename `slm_server`/references to e.g. `local-inference-gateway`, or keep-with-a-doc-note explaining the legacy name. Weigh cross-repo churn (separate repo + import paths + docs) vs clarity. Output a recorded *decision*, not silent drift. **Low priority.**

---

## 5. Source-doc claims the code REFUTED or already satisfies (do NOT file)

- **ARCH-1 "enforce delegation, the brain refuses to delegate"** — the deterministic decompose/delegate decision is *already* in the gateway (pure-function matrix, `decomposition.py`), and the context-firewall pattern is *already* implemented. Only the **grammar** half is a real gap → folded into ADR-0095. Do not file a generic "make it delegate" ticket.
- **ARCH-3 "let the brain retrieve memory as a tool (Letta-style)"** — that tool path *already exists* (`search_memory`, `recall_personal_history`). The open question is only the *default*, not building the capability → ADR-0096.
- **GOV-1 "don't let the loop self-approve"** — already structurally impossible (no self-action path). The only work is *documenting* the invariant (D1), not building a gate.
- **DB-1 "do we need Postgres"** — explicitly out of scope; the three-store split is justified. DB-1 is *only* the schema audit, whose concrete payload is the A1 bug + the residue audit (A2 below).
- **"Context firewall" terminology** — not in the repo; don't introduce it.

---

## 6. Residual schema-audit follow-up (after A1)

**A2. `[TICKET]` Postgres schema-debt audit — residue sweep** — **Low/Medium**, **[S]**
- Project: standalone (same home as A1). The non-bug remainder of DB-1.
- Findings to action: (1) `embeddings` table defined in `init.sql:176-192` (with HNSW index) but **never read/written** by any code — confirm dead, then drop or document as intentional-future. (2) `captains_log_captures`/`captains_log_reflections` Postgres tables — live data goes to **ES**, not Postgres (`captains_log/capture.py:152`); the Postgres tables are referenced only by lifecycle/cleanup tooling — confirm and decide keep/drop. (3) Confirm `metrics` and `consolidation_attempts` write paths are live. (4) Note that `api_costs`/cost_gate/`route_traces` persist via **raw asyncpg outside `service/repositories/`** by design (hot-path lock / identity boundary) — document, don't "fix."
- Acceptance: a short keep/drop/index note per table + any cheap corrective migrations; risky changes spun out as separate follow-ups.

---

## 7. Recommended sequencing (respects "infra/observability first")

1. **Now, ahead of streams (criticality):** A1 (the bug — verify live state, then fix). Cheap parallel: B1 (docs).
2. **adr session, in parallel (design-only, no deploy):** ADR-0094 → ADR-0095 → ADR-0096, each filing impl tickets only on approval. DOC-4 decision note alongside.
3. **Into the active L0 stream, owner-sequenced:** C1 (context-occupancy emit — on-theme, possibly interleave) then C2 (ablation flags).
4. **When self-improvement work resumes (Wave F):** D1, D2.
5. **Opportunistic:** A2 schema residue; REF-1/REF-2 captured as notes feeding C2.

**Guardrails for the master session:** New == Needs Approval (owner owns Approved). Don't jam these into Lane A/B queues — file and let the owner triage placement. Re-check ticket comments at every gate. For A1, the build session must `cd` into its worktree and verify the live DB read-only **before** writing the migration. ES field work in C1 must pre-walk dynamic_templates (the recurring first-pass-mappings trap).

---

## 8. Verification (how master confirms this plan landed correctly)

- **A1 fix:** `make test-infra-down && make test-infra-up`, then `docker exec <test-postgres> psql -U agent -d personal_agent -c '\d sessions'` shows `user_id`; create a session end-to-end against the fresh stack with no `column … does not exist` error; `init.sql` and `migrations/0011_*.sql` diff-match on the new column.
- **B1 docs:** `grep -ri "mlx\|apple silicon\|single-brain\|qwen3.5" README.md CLAUDE.md` returns only historical-ADR contexts; README diagram reads Qwen3.6 + hybrid.
- **C1 emit:** trigger a turn, confirm an ES doc with the four occupancy fields and `_field_caps` typed correctly; Kibana panel non-empty.
- **C2 flags:** flip `sub_agent_enabled=false` in the eval profile; confirm the eval run completes with the subsystem disabled and reports a delta.
- **ADRs:** each of 0094/0095/0096 lands as a PR (Proposed), reviewed by codex, with sequenced impl tickets filed on approval — per the adr skill, never touching src/.

---

*Tickets to create (master): A1, B1, C1, C2, D1, D2, A2 (7 tickets). ADRs to author (adr session): 0094, 0095, 0096 + DOC-4 decision note. Do-not-file: §5. Notes-only: REF-1, REF-2.*
