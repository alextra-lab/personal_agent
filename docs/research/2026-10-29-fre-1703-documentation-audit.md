# FRE-1703 — Documentation audit: the wrong statements in the documents that describe the system today

**Date:** 2026-10-29 · **Ticket:** FRE-1703 (step 1, read-only study) · **Seat:** cc-explore, commissioned by master with the owner's approval
**Code audited:** `main` at `e5c89ff4` (2026-10-26). The live gateway runs this revision (T1).
**Constraints kept:** no gateway turn, no model call, no paid call, no eval, no test run, no `.env` value (key names only).

## Summary

The study checked 58 in-scope documents against the code, `config/` and the running stack. It found **209 wrong statements**. Each one has a file and line, what the document says, what is true, and the source of the true fact.

| Verdict | Documents |
|---|---|
| Current | 9 |
| Has wrong statements | 41, of which 12 need a rewrite or a regeneration instead of line fixes |
| Obsolete (delete or move to the archive) | 8 |

The same stale facts repeat across many documents. Five of them account for the largest share:

1. **The model.** Documents name Qwen3.5-35B or Qwen3.6-35B as the single primary model, with no role switching. The live binding is `qwen3.8-flash-next`, the owner can choose another primary per session, and `config/model_roles.yaml` binds 14 roles, most of them to cloud models (T6).
2. **The model server.** Documents say MLX, LM Studio or a server on port 8000. The local model runs on llama.cpp on the owner's Mac (T4).
3. **The setting.** Documents say `LLM_BASE_URL` or `settings.llm_base_url`. The setting is `slm_base_url` (`AGENT_SLM_BASE_URL`), with no default (T4).
4. **The CLI.** Documents say `uv run agent "<message>"`. That form exits with an error. The command is `uv run agent chat "<message>"`.
5. **Removed designs.** Documents describe execution profiles (`config/profiles/`), the SSE `/stream/*` routes, `LocalLLMClient`, the `*_role` keys in `models.yaml` and Kibana. Each one was removed between 2026-05 and 2026-09.

Wrong statements by kind: path 47 · feature 37 · setting 31 · code name 16 · other 16 · service 12 · model 12 · count 10 · command 10 · index or table 9 · role 5 · port 3 · plus one row for the CLAUDE.md request-flow diagram (X1).

## How to read the findings

- **Each finding section covers one group of documents.** Its verdict is POSITIVE: the section lists wrong statements that a check found. A row is one wrong statement.
- **The `source` column is the query and its output.** A row cites a file and line on `main`, a truth-sheet fact (T1–T8), or a live command with its result.
- **Many rows contain a negative sub-claim, such as "this file does not exist".** Each section states the arms of the admissibility rule for those rows, once per section.
- **The "Unconfirmed" list in each section holds suspected errors that the study could not settle.** They are not counted in the 209.
- Row identifiers are A1–A28, X1, B1–B29, C1–C67, D1–D45 and E1–E39. The letter shows the reviewer group (see the method appendix).

## Ground truth — the live facts that the rows cite (T1–T8)

Verdict: POSITIVE. Each fact below comes from one query against the running stack or the `main` tree, run on 2026-10-29.

**T1 — the deployed code is `main`.** Query: SHA-256 of `/app/<file>` in container `cloud-sim-seshat-gateway`, compared with the `main` checkout. Output (first 12 characters, equal on both sides): `config/models.yaml` e3b260ed0c36 · `config/model_roles.yaml` c3a115f79bfe · `src/personal_agent/config/settings.py` 4a8a0edd3fc3 · `src/personal_agent/service/app.py` fe357f6fbdc8.

**T2 — the running containers.** Query: `docker ps`. Output, production stack (`docker-compose.cloud.yml`): caddy, cloudflared, elasticsearch 8.19.0 (:9200), filebeat, grafana 13.1.3 (host :3003), neo4j 5.26 (:7474, :7687), otel-collector 0.158.0, postgres pg17 (:5432), redis 7 (:6379), searxng (host :8888), seshat-gateway (host :9001), seshat-pwa (host :3000), tempo 2.10.7. The eval stack and the test stack also run. No local model server runs on the VPS.

**T3 — the gateway port.** The cloud gateway listens on 9001 (`Dockerfile.gateway:96`, `docker-compose.cloud.yml:427`). The `service_port` default is 9000 (`settings.py:1218`), which `make dev` uses on a local machine.

**T4 — the local model server is llama.cpp.** Query: Tempo search `{resource.service.name="slm-server"}` over the last 2 days, 20 traces, attribute `gen_ai.system`. Output: 20 of 20 spans carry `gen_ai.system = "llamacpp"`, span name `chat unsloth/qwen3.8-flash-next`, newest 2026-10-29T09:46:52Z. The setting is `slm_base_url` with no default (`settings.py:186-200`). `grep -rn 'LLM_BASE_URL\|llm_base_url' src config` returns one hit, the docstring at `settings.py:191` that says the old setting was replaced. Live gateway environment key names (names only) include `AGENT_SLM_BASE_URL`, `AGENT_SLM_TUNNEL_BASE_URL` and `SLM_HOST`.

**T5 — the model catalog (`config/models.yaml`).** Local deployments (provider `slm_local`): qwen3.6-35b-thinking, qwen3.8-flash-next, qwen3.8-27b-mtplx, qwen3.8-flash-next-mtplx, gemma-4-26b-a4b, reranker_fallback. Cloud deployments: qwen3.8-27b-ovh, glm-5.3-scaleway, deepseek-v4.1-flash-scaleway, claude_sonnet (`claude-sonnet-5`), claude_haiku, claude_haiku_5_5, claude_sonnet_5_5, gpt-5.4-mini, gpt-6-luna, gpt-6.1-sol, embedding (OVH `Qwen3-Embedding-8B`), reranker (Voyage `rerank-2.5`). Providers: slm_local, ovh, ovhcloud, scaleway, openai, anthropic, voyage.

**T6 — the 14 role bindings (`config/model_roles.yaml`).** primary → qwen3.8-flash-next (open) · artifact_builder → gpt-6.1-sol (open) · sub_agent → inherit (open) · entity_extraction → gpt-5.4-mini · captains_log → gpt-6.1-sol · session_summary, insights, span_extraction, entailment, vision → claude_sonnet · compressor → gpt-5.4-mini · embedding → embedding · reranker → reranker · reranker_fallback → reranker_fallback.

**T7 — the Makefile targets on `main`.** Query: `grep -oE '^[a-zA-Z0-9_.-]+:' Makefile`. Output: 60 targets (one internal, `_tunnel-guard`), including `dev`, `deploy`, `build`, `build-full`, `health`, `test`, `test-file`, `test-k`, `test-cov`, `test-integration`, `test-eval`, `eval`, `sandbox-build`, `render-prompt-corpus`, `prompt-show`, `test-infra-*`, `eval-infra-*`, `study-infra-*`, `tunnel-*`. There are five compose files: `docker-compose.yml`, `.cloud.yml`, `.eval.yml`, `.study.yml` and `.test.yml`.

**T8 — the scope rule.** Dated records are out of scope and are never listed as wrong: ADRs, `docs/research/`, `docs/plans/`, post-mortems, session logs and `docs/archive/`. One exception: an in-scope document that cites a dated or superseded record as the current design is in scope for that claim.

## Findings

### F1 — The entry documents: README, the CLAUDE.md files, docs/README, VISION_DOC and `make help`

**Documents:** `README.md`, `docs/README.md`, `docs/VISION_DOC.md`, `CLAUDE.md`, `.claude/CLAUDE.md`, `tests/CLAUDE.md`, and the `make help` text (Makefile:66-108).

**Verdict:** POSITIVE — 29 wrong statements.

**Arms for the negative sub-claims in this table:**

- **Arm 1 (identifier provenance).** The identifier in each negative row is the document's own text, quoted in the `says` column. For a moved path, the deletion commit in the `source` column shows that the path existed in the same store, the git tree. A10 and A21 cite paths that never existed: `git log --all -- <path>` returns 0 commits.
- **Arm 2 (path liveness).** Each search has a control with the same method that returns non-zero, quoted in the row. Examples: `grep -rn slm_base_url src config` → 41 against 1 for `LLM_BASE_URL`; `class LiteLLMClient` → `litellm_client.py:905` against 0 definitions of `class LocalLLMClient`; `docker/postgres/migrations/` → 36 files against 0 `alembic.ini`.
- **Arm 3 (scope).** Every repo search covers the `main` tree at `e5c89ff4`. Every live check names its container.

| # | file:line | says | true | source | kind |
|---|---|---|---|---|---|
| A1 | README.md:34 | PRIMARY AGENT `Qwen3.5-35-A3B` | primary = qwen3.8-flash-next, switchable per session; no Qwen3.5 deployment | config/model_roles.yaml:55; T5,T6; `grep -rni qwen3.5 config` → 3 comment hits only | model |
| A2 | README.md:76 | "Qwen3.5-35B as sole reasoning center — no role-switching" | 14 roles, mostly cloud; primary open:true | config/model_roles.yaml:46-155; T6 | role |
| A3 | README.md:73 | "Slices 1 & 2 Implemented" | Slices 1–3 implemented, Slice 3 done 2026-03-29 | docs/specs/COGNITIVE_ARCHITECTURE_REDESIGN_v2.md:4 | feature |
| A4 | README.md:85 | "Evaluation phase — building real usage traces before Slice 3" | Slice 3 done 2026-03-29 | same spec:4 | feature |
| A5 | README.md:81 | "expansion safety based on GPU/memory/concurrency state" | inputs are CPU, memory, active inference count; no GPU | brainstem/expansion.py:35-53; `grep -ci gpu` → 0, control `cpu` → 8 | feature |
| A6 | README.md:93 | "Multi-model routing via SLM Server (MLX-optimized)" | local server is llama.cpp | T4 | service |
| A7 | README.md:105 | "Apple Silicon Mac (for MLX-optimized models)" | llama.cpp on the owner's Mac; dev env is the Debian VPS | T2,T4 | service |
| A8 | README.md:150-151, 182-183 | `uv run agent "Hello, what should I focus on today?"` | bare message = unknown subcommand, exit 2; use `agent chat "<msg>"` | pyproject.toml:72; src/personal_agent/ui/service_cli.py:142-153 | command |
| A9 | README.md:253 | `LLM_BASE_URL=http://localhost:8000/v1` | setting is AGENT_SLM_BASE_URL, no default, origin without /v1 | settings.py:186-200; `grep LLM_BASE_URL\|llm_base_url src config` → 1 docstring hit; control slm_base_url → 41 | setting |
| A10 | README.md:267 | `tests/test_service/test_session_repository.py` | never existed; nearest tests/personal_agent/service/repositories/test_session_repository_retention.py | `git log --all -- <path>` → 0 commits | path |
| A11 | README.md:286-293 | `alembic revision --autogenerate` / `alembic upgrade head` | no Alembic; schema in docker/postgres/init.sql + migrations/ | `find -name alembic.ini` → 0; control migrations/ 36 files; .claude/CLAUDE.md:77 | command |
| A12 | README.md:339, 349 | `docs/SLM_SERVER_INTEGRATION.md` | moved to docs/guides/ | deleted 8f4a4838 2026-03-09 | path |
| A13 | README.md:353 | `docs/architecture/SERVICE_IMPLEMENTATION_SPEC_v0.1.md` | moved to docs/archive/ | deleted 1373c201 2026-03-30 | path |
| A14 | README.md:358-359 | `docs/superpowers/plans/2026-03-16-slice-1-foundation.md` (+slice-2) | moved to docs/plans/completed/ | deleted 5c68bb52 2026-03-28 | path |
| A15 | README.md:362, 408 | `docs/reference/CODING_CONVENTIONS.md` | moved to archive; live standard is docs/reference/CODING_STANDARDS.md | deleted b0153c1f 2026-03-30 | path |
| A16 | README.md:22 | "CLI . API /chat (port 9000) . Future: mobile, voice" | PWA web client is live; README never mentions it | T2 cloud-sim-seshat-pwa; `grep -n 'PWA\|pwa' README.md` → 0 | feature |
| A17 | docs/README.md:28 | `reference/CODING_CONVENTIONS.md` | broken link, file in docs/archive/ | deleted b0153c1f | path |
| A18 | docs/VISION_DOC.md:320 | writes proposals to `architecture_decisions/captains_log/` | writes to telemetry/captains_log/ | captains_log/manager.py:58-66; telemetry/lifecycle_manager.py:74-75 | path |
| A19 | CLAUDE.md:26 | `uv run agent "Your message here"` | unknown subcommand; use `agent chat` | as A8 | command |
| A20 | CLAUDE.md:23 | `ENV=cloud make ps # check VPS container status` (run from Mac) | runs docker compose locally, no SSH | Makefile:27-28, 131-132; vs CLOUD_EXEC Makefile:46-50 | command |
| A21 | CLAUDE.md:35 | `FILE=tests/test_tools/test_web.py` | never existed; test_web_search.py | `git log --all` → 0 | path |
| A22 | CLAUDE.md:77 | "LocalLLMClient → SLM Server on :8000 (separate repo, MLX-optimized)" | LocalLLMClient deleted (FRE-1367, a5b1729e 2026-09-04); LiteLLMClient to role-bound deployment; llama.cpp at AGENT_SLM_BASE_URL | 0 class defs; litellm_client.py:905; settings.py:186-200 | service |
| A23 | CLAUDE.md:103 | `LLM_BASE_URL=http://localhost:8000/v1` | AGENT_SLM_BASE_URL, no default, no /v1 | settings.py:186-200 | setting |
| A24 | CLAUDE.md:109 | "entity extraction (qwen3-8b)" | gpt-5.4-mini | model_roles.yaml:94 | model |
| A25 | .claude/CLAUDE.md:14 | "SLM Server is separate (MLX/Apple Silicon)" | llama.cpp on the owner's Mac | T4 | service |
| A26 | .claude/CLAUDE.md:110 | "Personal Agent `:9000` · SLM Server `:8000`" | VPS gateway :9001 (:9000 only make dev); no SLM on VPS | T2,T3,T4; Dockerfile.gateway:96 | port |
| A27 | .claude/CLAUDE.md:43 | "Dormant (last log 2026-04-16)" | last logs 2026-06-27, 2026-07-04 (8df8e51e) | `git log -1 -- docs/plans/sessions` | other |
| A28 | Makefile:83 (make help) | "run from the Mac — these SSH to the VPS, they do not run on it" | tunnel-* also run on the VPS (_ON_VPS → local compose) | Makefile:44-48, 200-214 | command |
| X1 | CLAUDE.md:66-73 | "Stage 2: session hydration", "Stage 3: governance" before "Stage 4: intent classification", Stage 7 trimming in the request flow | intent classification runs before governance; the caller passes session messages in; Stage 7 trims a copy of history that the executor does not send | src/personal_agent/request_gateway/pipeline.py:86-111; src/personal_agent/orchestrator/executor.py:5938-5942 (found by cc-explore while checking B3 and B10) | feature |

**Unconfirmed (not counted):**

- `CLAUDE.md:87`, "DECOMPOSE — task split into sequential sub-tasks". HYBRID and DECOMPOSE share one planner path with an `asyncio.TaskGroup` (`expansion_controller.py:2188`). The study did not trace whether DECOMPOSE runs its tasks in order.
- `README.md:126-137`, the SLM server `./start.sh` on port 8000. That code is in a separate repository.
- `README.md:229`, the event `memory_query_quality_metrics` in `agent-logs-*`. A free-text `_count?q=` returned 0, against a control of 4,343,842. A free-text query can miss a keyword field, so this is not a verdict.
- `CLAUDE.md:44`, "the full suite takes 7+ minutes". A check needs a test run, which the commission forbids.
- `README.md:174`, Grafana on :3000. This is true for the local compose file. On the VPS, :3000 is the PWA.

### F2 — The two architecture specs

**Documents:** `docs/specs/COGNITIVE_ARCHITECTURE_REDESIGN_v2.md` (CAR) and `docs/specs/SESHAT_PROGRAM_ARCHITECTURE.md` (SPA). `CLAUDE.md` names CAR as "the primary architecture reference".

**Verdict:** POSITIVE — 29 wrong statements.

**Arms for the negative sub-claims in this table:**

- **Arm 1 (identifier provenance).** Each negative row quotes the document's own name for the type, field, event, flag or tool. Where the code uses a different name, the row cites the real emit or definition site (for example `pipeline.py:97`, `:130`, `:234` for the gateway events).
- **Arm 2 (path liveness).** B6 is the one Elasticsearch negative. Query: `agent-logs-*/_count`, `term event_type`, `now-30d`. Output: each of the five spec names (`security_check`, `governance_decision`, `intent_classification`, `decomposition_decision`, `context_budget`) → 0. Controls with the same query: `intent_classified` 239, `decomposition_assessed` 471, `gateway_output` 238 (counts are provisional, FRE-1051). Repo searches carry a control in the row, for example `def suggest_relevant(` → 2 against 0 for `def demote(`.
- **Arm 3 (scope).** Repo searches cover `main` at `e5c89ff4`. B6 covers `agent-logs-*` over the 30 days before 2026-10-29. B7 rests on the absence of the key name `AGENT_DELEGATION_ENABLED` in the live gateway environment. Another configuration route cannot be excluded without reading values, so B7 states only that the default is False and that the key is absent.

| # | file:line | says | true | source | kind |
|---|---|---|---|---|---|
| B1 | CAR:72,148 (+ "35B" primary at 199,301,341,386,562,800,1308,1450) | single capable model (Qwen3.6-35B-A3B) is the reasoning center | primary qwen3.8-flash-next, switchable; qwen3.6-35b-thinking bound to no role | T5,T6 | model |
| B2 | CAR:4 | Slices 1–3 Implemented | resolve_role() still exists (identity); demote/forget/check_contradiction 0 defs; task-type skill loading replaced | orchestrator/routing.py:70; grep defs → 0 (control suggest_relevant → 2) | feature |
| B3 | CAR:134, 365-377 | Security → Session → Governance → Intent; GovernanceContext {allowed_tools, cost_budget_remaining} | intent before governance; governance mode-only; no allowed_tools (has expansion_budget); cost_budget_remaining never set | request_gateway/pipeline.py:92-111; governance.py:1-5,45-49; types.py:80-94 | code-name |
| B4 | CAR:346-354 | rate limiting, sanitization, PII detection, ALLOW/REJECT | Stage 1 pass-through stub; no security.py | pipeline.py:4,87-88 | feature |
| B5 | CAR:362 | Output: SessionContext | caller passes messages; no SessionContext type | pipeline.py:5,90; grep → 0 (control SessionManager 5) | code-name |
| B6 | CAR:354,377,408,442,499 | events security_check, governance_decision, intent_classification, decomposition_decision, context_budget | emitted: intent_classified, decomposition_assessed, governance_evaluated, gateway_output | pipeline.py:97,130,234; governance.py:37; ES agent-logs-* 30d term event_type: five spec names → 0 each; intent_classified 239, decomposition_assessed 471, gateway_output 238 (provisional) | code-name |
| B7 | CAR:425 (Flow 3 245-246) | DELEGATION any → DELEGATE | only when delegation_enabled (default False; no AGENT_DELEGATION_ENABLED key live) | decomposition.py:176-198; settings.py:660-668; env key names | feature |
| B8 | CAR:422 | ANALYSIS MODERATE → SINGLE or HYBRID | always HYBRID; TOOL_USE SINGLE always; planner mode → HYBRID planner_asked | decomposition.py:160-164,195-206; models.yaml:309 | feature |
| B9 | CAR:487-499, 515-522 | compress older history (summarize), DECOMPOSE override, BudgetedContext, budget_overflow | drops oldest history, then memory, then tools; no summary, no re-entry; types do not exist | request_gateway/budget.py:1-9; grep → 0 | feature |
| B10 | CAR:216-217 | Budget trim → Primary Agent responds using assembled context | executor rebuilds messages from SessionManager; Stage 7 trims a discarded copy | orchestrator/executor.py:5938-5942; b0600ec3 (FRE-1135) | feature |
| B11 | CAR:508-513 | security_degraded, session_ephemeral, memory_degraded flags | do not exist; degraded_stages list | grep → 0; pipeline.py:85,222 | code-name |
| B12 | CAR:318 | > loop_max_per_signature → BLOCK_IDENTITY | ADVISE_IDENTITY above max; BLOCK only above max+2 | orchestrator/loop_gate.py:8-12,50-51 | feature |
| B13 | CAR:320-322 | WARN at N, BLOCK at N+1; IDLE→ACTIVE→WARNED→BLOCKED | advisory; BLOCK_CONSECUTIVE at N only with loop_consecutive_terminal; no WARNED state | loop_gate.py:32-37,53-55,215-235; governance/tools.yaml:661,674 | code-name |
| B14 | CAR:329-331 | run_sysdiag, self_telemetry_query, infra_health set loop_output_sensitive | tools do not exist; only bash sets it | tools.yaml grep → 0 each (control search_memory 1); :665,673 | code-name |
| B15 | CAR:164,200 | native tools search_memory, system_health | system_health deleted 2026-05-12 (FRE-265) | git log → a29b2c21 | code-name |
| B16 | CAR:590-600 | each key in models.yaml, entity_extraction_role, None routes to LocalLLMClient | roles in model_roles.yaml; LocalLLMClient deleted; LiteLLMClient | models.yaml:40-50; context_compressor.py:48-50; llm_client/factory.py:114-116 | setting |
| B17 | CAR:617-659, 669-672, 458 | Stage 6 maps task type to skills | skills/<name>/SKILL.md catalog + read_skill (ADR-0156 D2); listed dirs absent; AssembledContext.skills never set | orchestrator/skills.py:1-10,29; grep "skills=" context.py → 0 | feature |
| B18 | CAR:841-843 | evaluates GPU utilization, error rate | CPU, memory, active_inference_count | brainstem/expansion.py:35-52 | feature |
| B19 | CAR:876-894 | contradiction detection, confidence, memory types, proactive, abstract protocol "Missing" | all exist | memory/supersession.py, weight.py, protocol.py:21-40,198, proactive.py | feature |
| B20 | CAR:918-920 | Profile storage PostgreSQL user_interests | no such table; Neo4j | live psql to_regclass('public.user_interests') → null, control sessions → sessions; memory/service.py:7021-7040 | other |
| B21 | CAR:935-1013 | MemoryProtocol with store_fact, demote, forget… | real methods recall, recall_broad, store_episode, promote, is_connected, suggest_relevant, resolve_message_entities, get_current_stances; 8 others 0 defs | memory/protocol.py:90,122,198-360 | code-name |
| B22 | CAR:89 (+526,1193,1322,1375,1417) | Elasticsearch/Kibana dashboards | Kibana retired; Grafana only | docker-compose.cloud.yml:190-191; docker ps kibana 0, grafana 1 | service |
| B23 | CAR:1530 | LM Studio single-model bottleneck | llama.cpp on the Mac | T4 | service |
| B24 | CAR:1341-1345 | orchestrator/types.py new TaskType, GatewayOutput; security.py; service.py implements MemoryProtocol | request_gateway/types.py; no security.py; MemoryServiceAdapter | request_gateway/types.py:21,63,97,163; memory/protocol_adapter.py:61 | path |
| B25 | CAR:1546,1551 | docs/architecture/…v0.1.md | moved to docs/archive/ | 1373c201 2026-03-30 | path |
| B26 | SPA:273-274, 194-195 | deep recall gated to CONVERSATIONAL (recall_controller.py:173) | file deleted 2026-08-28 (FRE-1135); MEMORY_RECALL broad recall, others entity+proactive | b0600ec3; request_gateway/context.py:646-688 | path |
| B27 | SPA:196-197 | model_tier field on GatewayOutput | never shipped (ADR-0082 D1 proposal) | types.py:163-185; grep → 0 | code-name |
| B28 | SPA:130-145 | turn_status emitted from executor; ADR-0088 must pick one | ADR-0088 shipped; cg:turn-projector sole emitter (FRE-513) | observability/topology/__init__.py:1-6; service/app.py:1440-1446 | feature |
| B29 | SPA:211-213 | move from sanitize output, strip enforcement off as temporary bridge | done: sanitizer retired, sandbox live | ADR-0089 Status line 3; seshat-pwa/src/components/ArtifactViewer.tsx | feature |

**Unconfirmed (not counted):**

- CAR:474-481, the hardware budget (M4 Max, 32-64K comfortable). The Mac cannot be inspected from the VPS.
- CAR:1373 and :1416, the Graphiti experiment and the backend decision. No code exists for either. They may be research records.
- CAR:808-833, the sub-agent fan-out rules. The cited tickets appear in `expansion_controller.py:920-1002`. The study did not trace `_fan_out_limit` end to end.
- SPA:186, the FRE-453 eval-set content.

### F3 — The guides (`docs/guides/`, 18 files)

**Documents:** Every file under `docs/guides/` (`find docs/guides -type f` → 18 files).

**Verdict:** POSITIVE — 67 wrong statements.

**Arms for the negative sub-claims in this table:**

- **Arm 1 (identifier provenance).** Each negative row quotes the guide's own route, field, file, event or setting. Five shared facts carry the provenance for many rows:
  - un-prefixed setting names are ignored, because `settings.py:53` sets `env_prefix="AGENT_"` (a test with pydantic-settings 2.15.0: `LLM_TIMEOUT_SECONDS=7` leaves the default of 120, and `AGENT_LLM_TIMEOUT_SECONDS=7` sets 7);
  - `transport/agui/endpoint.py`, the SSE endpoint, was deleted in `e7f8a1cf` (2026-05-27);
  - `config/profiles/` was deleted in `643e8503` (2026-07-20);
  - the Terraform moved to a private repository in `ede28405` (2026-05-01);
  - the CLI entry point is `agent = personal_agent.ui.service_cli:app` (`pyproject.toml:72`).
- **Arm 2 (path liveness).** Controls with the same method: `'"/ws/{session_id}"'` → 1 (`transport/agui/ws_endpoint.py:741`) against 0 for `'"/stream'`; `def get_llm_client` → 2 against 0 for `load_profile`; `api_costs` in `init.sql` → 11 against 0 for `api_calls`; `^    provider:` in `models.yaml` → 18 against 0 for `provider_type:`.
- **Arm 3 (scope).** Repo searches cover `main` at `e5c89ff4`. Index and table rows cover the live production Elasticsearch (`_cat/indices`, `_ilm/policy`) and Postgres (`\dt`, `\d <table>`) on 2026-10-29.

| # | file:line | says | true | source | kind |
|---|---|---|---|---|---|
| C1 | CONFIGURATION.md:10, 47-57, 140 | edit LLM_BASE_URL if not using LM Studio | AGENT_SLM_BASE_URL, no default, llama.cpp | E2; settings.py:186-197; T4 | setting |
| C2 | CONFIGURATION.md:65,72,81-82,89-91,98-99,106-107,117-118,141-145 | LLM_TIMEOUT_SECONDS=180, ORCHESTRATOR_MAX_CONCURRENT_TASKS=10, MCP_GATEWAY_ENABLED=true… | un-prefixed names ignored; need AGENT_ (only APP_LOG_LEVEL/FORMAT/DEBUG aliases) | E1; settings.py:65,152,156 | setting |
| C3 | CONFIGURATION.md:90 | task timeout Default 300 | 900 | settings.py:225-226 | setting |
| C4 | CONFIGURATION.md:91 | max tool iterations Default 3 | 25 | settings.py:283-284 | setting |
| C5 | CONFIGURATION.md:99 | MCP_GATEWAY_TIMEOUT_SECONDS=60 # Default: 30 | 60 | settings.py:538-539 | setting |
| C6 | CONFIGURATION.md:34 | models.yaml … (primary, sub_agent, compressor roles) | roles in model_roles.yaml | models.yaml:56,124; model_roles.yaml:55-155 | path |
| C7 | CONFIGURATION.md:110-111 | compressor gpt-5.4-nano | gpt-5.4-mini; nano 0 hits | model_roles.yaml:123 | role |
| C8 | CONFIGURATION.md:127-128 | OPENAI_API_KEY / ANTHROPIC_API_KEY | AGENT_OPENAI_API_KEY / AGENT_ANTHROPIC_API_KEY (live names) | settings.py:1633,1638 | setting |
| C9 | CONFIGURATION.md:145 | MODEL_CONFIG_PATH | field no longer exists | settings.py:3646-3648; control governance_config_path 1 | setting |
| C10 | MCP_INTEGRATION_QUICK_START.md:17, 42 | MCP_GATEWAY_COMMAND | AGENT_MCP_GATEWAY_COMMAND (live name) | settings.py:534 | setting |
| C11 | MAC_OTEL_COLLECTOR.md:130-131, 159-161 | ingress host does not exist until FRE-1223 | FRE-1223 Done 2026-08-16; OTLP_HOST in Caddy; spans reach Tempo | docker-compose.cloud.yml:489; Caddyfile:173; T4 | feature |
| C12 | GPU_METRICS_SECURITY.md:117 | macmon detected but not parsed | parsed, tried first | brainstem/sensors/platforms/apple.py:36,364-368 | feature |
| C13 | GPU_METRICS_SETUP.md:160 | macmon broken … (Not Used) | used before powermetrics | apple.py:344-368 | feature |
| C14 | GPU_METRICS_SETUP.md:59 | python -m personal_agent.ui.cli "Hello…" | ui/cli is telemetry-only; use agent chat | E6 | command |
| C15 | GPU_METRICS_SETUP.md:62 | check logs for gpu_metrics_via_powermetrics | no such event (0; control powermetrics_invalid_json 1) | apple.py:284 | other |
| C16 | TELEMETRY_ELASTICSEARCH_INTEGRATION.md:22 | daily indices agent-logs-YYYY.MM.DD | monthly agent-logs-2026-07..10 | _cat/indices | index/table |
| C17 | TELEMETRY_ELASTICSEARCH_INTEGRATION.md:166-167, 174-180 | live migration in progress (FRE-1036) | done; 72 daily leftovers (captures/projector-health/topology, newest 2026-07-31); agent-logs monthly only | _cat/indices | index/table |
| C18 | TELEMETRY_ELASTICSEARCH_INTEGRATION.md:283 | daily indices with 30-day retention | monthly; ILM 123d / 180d / 365d | _ilm/policy | index/table |
| C19 | TELEMETRY_ELASTICSEARCH_INTEGRATION.md:80, 236 | Grafana http://localhost:3000 | :3000 is PWA; Grafana :3003 | T2 | port |
| C20 | USAGE_GUIDE.md:61, 64, 260 | uv run agent "Hello…" | the message is parsed as an unknown subcommand and the CLI exits with an error; use agent chat | E6; as A8 | command |
| C21 | USAGE_GUIDE.md:116-127 | response {content, usage{prompt_tokens}} | {session_id, response, trace_id, primary_selection, seshat_effort} | service/app.py:2777-2783 | feature |
| C22 | USAGE_GUIDE.md:150 | profile=cloud form field | no profile field | app.py:2952-2960 | feature |
| C23 | USAGE_GUIDE.md:218-221 | LLM_BASE_URL, LLM_TIMEOUT_SECONDS, LLM_MAX_RETRIES | no LLM_BASE_URL; others need AGENT_ | E1, E2 | setting |
| C24 | USAGE_GUIDE.md:46-47, 387-390 | curl localhost:8000/health; /v1/backends/health | no model server on agent host | T2, T4 | service |
| C25 | USAGE_GUIDE.md:428-434 | from personal_agent.tools.types import ToolExecutor, ToolResult | ToolExecutor 0; ToolResult needs tool_name, latency_ms | tools/types.py:124-133 | other |
| C26 | USAGE_GUIDE.md:440-449 | models: router: qwen/qwen3-1.7b backend mlx port 8500 | no router, no backend/port fields | models.yaml:56-123; T5 | model |
| C27 | USAGE_GUIDE.md:456 | Grafana localhost:3000 | :3003 | T2 | port |
| C28 | USAGE_GUIDE.md:462 | [Architecture Documentation](architecture/) | docs/guides/architecture/ missing | ls | path |
| C29 | AGUI_STREAMING_REFERENCE.md:13,21,30,58-75 | SSE GET /stream/{session_id} | WebSocket /ws/{session_id} | E3 | feature |
| C30 | AGUI_STREAMING_REFERENCE.md:79-87, 162 | POST /stream/{id}/resume for HITL | route does not exist | E3 | feature |
| C31 | AGUI_STREAMING_REFERENCE.md:44, 246 | profile local/cloud | primary_selection | app.py:2952-2960 | feature |
| C32 | AGUI_STREAMING_REFERENCE.md:209 | transport/agui/endpoint.py manages queues | deleted 2026-05-27 | E3 | path |
| C33 | AGUI_STREAMING_REFERENCE.md:253 | connectToStream( | connectWebSocket | seshat-pwa/src/lib/agui-client.ts:363 | other |
| C34 | AGUI_STREAMING_REFERENCE.md:305-308 | set_current_profile(load_profile(…)) | 0 | E4 | feature |
| C35 | AGUI_STREAMING_REFERENCE.md:341-348 | @backend path … /stream/* | /ws/* | Caddyfile:18 | path |
| C36 | AGUI_STREAMING_REFERENCE.md:176-207 | 5 internal event dataclasses | 12 | transport/events.py:75-317 | count |
| C37 | CLOUD_DEPLOYMENT.md:37, 261 | /stream/* | /ws/* | Caddyfile:18 | path |
| C38 | CLOUD_DEPLOYMENT.md:82 | Debian 12 (Bookworm) | Debian 13 (trixie) | /etc/os-release | other |
| C39 | CLOUD_DEPLOYMENT.md:140-174 | infrastructure/terraform/ main.tf | private repo since 2026-05-01 | E5 | path |
| C40 | CLOUD_DEPLOYMENT.md:107-114, 343-366 | required variables, 5 | compose :? requires 7; §8 omits 5 | grep '${…:?' docker-compose.cloud.yml | setting |
| C41 | CLOUD_DEPLOYMENT.md:205 | seshat-gateway 768 MB | mem_limit 2g | docker-compose.cloud.yml:428 | count |
| C42 | CLOUD_DEPLOYMENT.md:197-210 | 8 live services, ~5.3 GB | 13 services; omits tempo, otel-collector, grafana, searxng, filebeat | compose; T2 | count |
| C43 | CLOUD_DEPLOYMENT.md:272-274 | site block localhost only | 6 site blocks | Caddyfile:71,81,100,146,173,206 | other |
| C44 | CLOUD_DEPLOYMENT.md:432-468 | profiles in config/profiles/, per conversation | removed 2026-07-20; primary role open selection | E4; model_roles.yaml:55 | feature |
| C45 | CLOUD_DEPLOYMENT.md:459-460 | claude-sonnet-4-6 | claude-sonnet-5 | T5 | model |
| C46 | CLOUD_DEPLOYMENT.md:535 | FileNotFoundError config/profiles/cloud.yaml | cannot occur | E4 | path |
| C47 | SCHEMA_REFERENCE.md:4, 628-707 | Terraform variable schema | not in this repo | E5 | path |
| C48 | SCHEMA_REFERENCE.md:15-16,31,37,92-138,351 | GET /stream, POST /stream/…/resume | WebSocket /ws | E3 | feature |
| C49 | SCHEMA_REFERENCE.md:45 | profile field | none | app.py:2952-2960 | feature |
| C50 | SCHEMA_REFERENCE.md:49-55 | /chat/stream {session_id, trace_id, status} | {session_id, status, primary_selection, selection_provenance, seshat_effort, effort_provenance} | app.py:3078-3085 | feature |
| C51 | SCHEMA_REFERENCE.md:72 | uv run agent "..." via service_client.py | agent chat, ui/service_cli.py | E6 | command |
| C52 | SCHEMA_REFERENCE.md:241-245 | /health {"status":"ok"} | {"status":"healthy", build_fingerprint, components} | app.py:1951-1967 | feature |
| C53 | SCHEMA_REFERENCE.md:21, 424-472 | config/profiles/local.yaml, cloud.yaml | deleted | E4 | path |
| C54 | SCHEMA_REFERENCE.md:482-485 | top-level entity_extraction_role … keyed by role | providers/models keyed by deployment | models.yaml:56,124; T6 | path |
| C55 | SCHEMA_REFERENCE.md:497, 530, 538 | provider_type local/managed/cloud | provider + dialect; 0 vs control provider: 18 | llm_client/models.py:45 | setting |
| C56 | SCHEMA_REFERENCE.md:503 | endpoint default settings.llm_base_url | settings.slm_base_url | models.yaml:21 | setting |
| C57 | SCHEMA_REFERENCE.md:528, 536 | providers anthropic/openai/google; claude-sonnet-4-6 | 7 providers, no google; claude-sonnet-5 | T5 | model |
| C58 | SCHEMA_REFERENCE.md:23, 547-551 | config/models.cloud.yaml merged over models.yaml | deleted 1ee37c62 2026-07-19 | git log | path |
| C59 | SCHEMA_REFERENCE.md:570-571 | metadata, messages not nullable | nullable | live \d sessions; init.sql:14-15 | index/table |
| C60 | SCHEMA_REFERENCE.md:560-576 | sessions 13 columns | missing revealed_tools, seshat_effort | live \d sessions | index/table |
| C61 | SCHEMA_REFERENCE.md:574 | execution_profile server-authoritative | vestigial, always 'local', never read | app.py:464-467 | feature |
| C62 | SCHEMA_REFERENCE.md:611-624 | api_calls table; cost_usd numeric(10,6) | api_costs; numeric(18,12); more columns | live \d api_costs; init.sql:108-122 | index/table |
| C63 | SCHEMA_REFERENCE.md:6, 555-624 | every schema boundary … 3 tables | 18 tables + sysgraph schema | live \dt | count |
| C64 | SLM_SERVER_INTEGRATION.md:1-281 | MLX, ports 8500-8503, llm_base_url | OBSOLETE: llama.cpp on Mac; slm_base_url | T2,T4,T5; llm_client/factory.py:35 | model |
| C65 | EVALUATION_PHASE_GUIDE.md:1-184 | Slices 1&2 → Evaluation (2026-03-21), Kibana :5601 | OBSOLETE: Kibana retired; cited files deleted | compose:191; be210503; 443a9122 | service |
| C66 | IMPLEMENTATION_QUICK_REFERENCE.md:1-427 | complete implementation specifications (2026-01-17) | OBSOLETE: finished plan, specs archived, links missing | ls; docs/archive | path |
| C67 | METRICS_STORAGE_GUIDE.md:1-339 | not persisted by default | OBSOLETE: request_metrics_summary 714 docs in agent-logs-*; metrics table | ES _count; \d metrics | feature |

**Unconfirmed (not counted):**

- `CLOUD_DEPLOYMENT.md:56` and `:151-160`, the OVH firewall rules. The Terraform is in a private repository.
- `MAC_OTEL_COLLECTOR.md:3` and `:13-15`, the exporter in `src/slm_server/telemetry.py`. That code is in a separate repository.
- `USAGE_GUIDE.md:24-31`, how the Mac model server starts today.
- `AGUI_STREAMING_REFERENCE.md:107` and `SCHEMA_REFERENCE.md:283`, "a single TEXT_DELTA". `litellm_client.py:681` handles `stream=True`, so token streaming may exist. Not traced end to end.
- `TELEMETRY_ELASTICSEARCH_INTEGRATION.md:44` and `:126`, the event `system_metrics_snapshot`. `_count` with `term event_type` returned 0. The instrument may be wrong, so this is not a verdict.

### F4 — The reference documents (`docs/reference/`, without CODING_STANDARDS, PROMPT_CORPUS and artifact-builder)

**Documents:** The 18 files directly under `docs/reference/`, without `CODING_STANDARDS.md` and `PROMPT_CORPUS.md` (see F5).

**Verdict:** POSITIVE — 45 wrong statements.

**Arms for the negative sub-claims in this table:**

- **Arm 1 (identifier provenance).** Each negative row quotes the document's own key, file, class member, label or path. Deleted files carry their deletion commit (for example `1ee37c62` for `models.cloud.yaml`, `643e8503` for `config/profiles/`, `a29b2c21` for the May tool removals). ROADMAP.md (D21, D41, D42) never existed: `git log -- ROADMAP.md` → 0.
- **Arm 2 (path liveness).** Controls with the same method, quoted in the rows: `gpt-5.4-mini` in `models.yaml` → 4 against 0 for `gpt-5.4-nano`; `README.md` in git log → 8 against 0 for `ROADMAP.md`; `AGENTS.md` files → 17 against 0 `.agent.md`; Linear label `PersonalAgent` present against none of `Needs Approval`, `Review OK`, `Review Needs Work` (26 labels, one full page).
- **Arm 3 (scope).** Repo searches cover `main` at `e5c89ff4`. D25-D27 cover the live production Elasticsearch ILM policies on 2026-10-29 (`agent-logs-policy` delete phase `min_age` = 123d). D43 covers the FrenchForest team labels.

| # | file:line | says | true | source | kind |
|---|---|---|---|---|---|
| D1 | ENTITY_EXTRACTION_MODELS.md:9-11 | Qwen 3 8B DEFAULT, `entity_extraction_role: reasoning` in models.yaml | entity_extraction → gpt-5.4-mini | config/model_roles.yaml:94; T6 | role |
| D2 | ENTITY_EXTRACTION_MODELS.md:146 | selected by top-level key `entity_extraction_role` in models.yaml | no such key; resolve_role_model_key("entity_extraction") reads model_roles.yaml | grep ^entity_extraction_role models.yaml = 0 (comment at :811 only); second_brain/entity_extraction.py:1100 | setting |
| D3 | ENTITY_EXTRACTION_MODELS.md:148 | role names under models: (reasoning, router, standard, coding) | keys are deployments | config/models.yaml:124-; T5 | model |
| D4 | ENTITY_EXTRACTION_MODELS.md:169 | `ModelRole.REASONING` | no REASONING member | llm_client/types.py:18-40 | other |
| D5 | ENTITY_EXTRACTION_MODELS.md:173-175 | Current Configuration: reasoning — qwen3.6-35b-a3b | gpt-5.4-mini | model_roles.yaml:94 | model |
| D6 | ENTITY_EXTRACTION_MODELS.md:207-212 | use reasoning 35B for all extraction, don't use Claude | production uses a cloud model | model_roles.yaml:94 | role |
| D7 | SLM_SERVER_CLIENT_SEMANTICS.md:223-225 | primary omits max_tokens; instruct entry sets 2048 | flash-next max_tokens 12288; -instruct entry deleted (FRE-1445) | config/models.yaml:259, :244-246 | setting |
| D8 | SLM_SERVER_CLIENT_SEMANTICS.md:228-229 | context_length 262144 on both flash-next entries | 131072 (FRE-1427) | models.yaml:235, :389 | setting |
| D9 | CONFIG_INVENTORY.md:20 | settings.py (277 fields) | 361 | AST count; doc's own :47 | count |
| D10 | CONFIG_INVENTORY.md:21-22 | models.cloud.yaml, models-baseline.yaml, models.benchmark-*.yaml, models.medium.yaml | none exist; one catalog | deleted 1ee37c62 2026-07-19, 17114045 2026-07-04 | path |
| D11 | CONFIG_INVENTORY.md:24, 646-657 | config/profiles/{local,cloud}.yaml | deleted 643e8503 2026-07-20 (FRE-920) | ls config/profiles → none | path |
| D12 | CONFIG_INVENTORY.md:25, 667 | budget.yaml fields | only budget.yaml.example tracked (FRE-1238, 204e44f4 2026-08-09) | git ls-files config/governance | path |
| D13 | CONFIG_INVENTORY.md:630 | gpt-5.4-nano row (live) | no such deployment | grep gpt-5.4-nano models.yaml = 0; control mini = 4 | model |
| D14 | CONFIG_INVENTORY.md:633, 719 | drift persists in three benchmark-*.yaml | deleted 2026-07-19 | find = 0 | path |
| D15 | CONFIG_INVENTORY.md:639-640, 720 | embedding Qwen3-Embedding-0.6B; reranker 0.6B local vs 4B cloud | embedding Qwen3-Embedding-8B (ovh), reranker voyage rerank-2.5, fallback Qwen3-Reranker-4B-mxfp8 | models.yaml:993, 1013, 1029; T5 | model |
| D16 | CONFIG_INVENTORY.md:601 | fields on ModelConfig (llm_client/models.py:298-306) incl entity_extraction_role | no such field | grep = 0; control class ModelConfig = 1 | setting |
| D17 | CONFIG_INVENTORY.md:698 | .env.example 35 KB, 166 AGENT_* keys, all bar one commented | 49,369 bytes; 193 keys; 3 uncommented | ls -la; grep counts (names only) | count |
| D18 | CONFIG_INVENTORY.md:702 | 113 fields have no .env.example line | own §1 says 163 | CONFIG_INVENTORY.md:419 | count |
| D19 | CONFIG_INVENTORY.md:708 | 7 fields match secret heuristic | 16 per own §1 :595 | settings.py:1550-1651 | count |
| D20 | PROJECT_DIRECTORY_STRUCTURE.md:15 | config/ gitignored except templates; config/*.template | YAMLs tracked; no *.template | git check-ignore rc=1 | path |
| D21 | PROJECT_DIRECTORY_STRUCTURE.md:38, 152 | ROADMAP.md at root | does not exist, never in history | find = 0; git log = 0; control README = 8 | path |
| D22 | PROJECT_DIRECTORY_STRUCTURE.md:41-54 | top-level dirs lists 9 | also scripts, seshat-pwa, skills, infrastructure, e2e, models, tools | ls root | count |
| D23 | PROJECT_DIRECTORY_STRUCTURE.md:19 | architecture_decisions/ … captains_log/ | no such dir | ls; git log = 0 | path |
| D24 | PROJECT_DIRECTORY_STRUCTURE.md:260-264 | .gitignore telemetry/, config/*.yaml, !config/*.yaml.template | no such lines | grep .gitignore = 0; control telemetry/logs/ = 1 | path |
| D25 | DATA_LIFECYCLE.md:92-94 | agent-logs 30d retention; 30d–365d | agent-logs 123d; families 123d–365d; only slm-requests 30d | live _ilm/policy; docker/elasticsearch/ilm-policy.json | index/table |
| D26 | DATA_LIFECYCLE.md:96-98 | captures/reflections 90d/180d | captures 123d (reflections 180d correct) | live agent-captains-captures-policy | index/table |
| D27 | DATA_LIFECYCLE.md:23 | ES indices 30d–365d | 123d–365d | as D25 | index/table |
| D28 | CAPTAINS_LOG_HUMAN_APPROVAL_INVARIANT.md:11,15,25 | promotion.py:603-629, :614 | APPROVED write is _mark_promoted :880-958 (status :930) | captains_log/promotion.py:579, 617, 880, 930 | path |
| D29 | CAPTAINS_LOG_HUMAN_APPROVAL_INVARIANT.md:14 | promotion.py:106-181 | _format_linear_description at :131 | promotion.py:100, 113, 131 | path |
| D30 | DEPENDENCY_SECURITY.md:9 | cryptography overridden to >=46.0.5 | >=50.0.0 | pyproject.toml:110 | setting |
| D31 | POSTGRES_SCHEMA_DEBT_AUDIT.md:65-67 | service/repositories/ only holds 4 files | also grounding_compliance_ and grounding_enforcement_repository.py | ls | count |
| D32 | TESTING_STANDARDS.md:30, 45, 100 | LM Studio on 127.0.0.1:1234 | no LM Studio; llama.cpp via AGENT_SLM_BASE_URL; a test asserts :1234 gone | T4; tests/test_config/test_slm_endpoint_resolution.py:4, 106-112 | service |
| D33 | TESTING_STANDARDS.md:40, 101, 106 | entity_extraction_role: reasoning → qwen3.6-35b | gpt-5.4-mini | model_roles.yaml:94 | role |
| D34 | TESTING_STANDARDS.md:60, 68, 148, 155 | LocalLLMClient() at line 144, ModelRole.REASONING | both gone; reflection uses ModelRole.CAPTAINS_LOG (gpt-6.1-sol) | reflection.py:535, 651; context_compressor.py:50 | other |
| D35 | TESTING_STANDARDS.md:120 | @patch("…executor.LocalLLMClient") | target does not exist | grep executor.py = 0 | path |
| D36 | DELIVERY_PIPELINE.md:67-68, 194 | pre-tool hook enforces global test lock | removed 2026-07-18 | CLAUDE.md Testing; .claude/hooks listing | feature |
| D37 | TOOL_INTEGRATION_GUIDE.md:66 | elasticsearch.py example | deleted FRE-265 a29b2c21 2026-05-12 | ls tools/ | path |
| D38 | TOOL_INTEGRATION_GUIDE.md:217, 222-225 | Tier 1 tools query_elasticsearch, read_file, list_directory, system_metrics_snapshot, self_telemetry_query | none exist; current tools search_logs, linear, primitives, read_skill | grep name= = 0 each, control web_search = 1; tools/__init__.py:88-195 | feature |
| D39 | VALIDATION_CHECKLIST.md:122 | experiments/E-XXX-*.md | in docs/architecture_decisions/experiments/ | ls | path |
| D40 | VALIDATION_CHECKLIST.md:168 | captains_log/CL-*.md | none; JSON in telemetry/captains_log + ES | find = 0 | path |
| D41 | VALIDATION_CHECKLIST.md:202 | ROADMAP | none | D21 | path |
| D42 | ROOT_LEVEL_POLICY.md:19, 64-75 | ROADMAP.md; expected root list | no ROADMAP; root also Makefile, CLAUDE.md, LICENSE, 3 Dockerfiles, 5 compose, sgconfig.yml, init script | ls -a | path |
| D43 | AGENT_WORKFLOW_METHODOLOGY.md:133, 140, 322 | labels Needs Approval, Review OK, Review Needs Work | none in FrenchForest; approval is state-only | Linear list_issue_labels (26): none; control PersonalAgent | other |
| D44 | AGENT_MD_STRATEGY.md:449 | [x] Create root .agent.md | 0 .agent.md files | find = 0; control AGENTS.md = 17 | path |
| D45 | AGENT_MD_STRATEGY.md:111, 135, 142-143 | docs/CODING_STANDARDS.md, ./run_tests.sh, .cursorrules, docs/PR_REVIEW_RUBRIC.md | none exist | ls | path |

**Unconfirmed (not counted):**

- `CONFIG_INVENTORY.md` §1, "163 fields undocumented" and the orphan `AGENT_HOST`. The study did not run `config_inventory.py verify`, because it imports the package. The §1 field table matches the code: 361 names, 0 missing, 0 extra.
- `DEPENDENCY_SECURITY.md:10`, CVE-2025-69872. `pyproject.toml:123` names PYSEC-2026-2447. These may be aliases of one advisory.
- `DELIVERY_PIPELINE.md:130`, CodeQL. It is not in `.github/workflows/`. It may be GitHub default setup.
- `TESTING_STANDARDS.md`, "~71s". A check needs a test run.
- `SLM_SERVER_CLIENT_SEMANTICS.md` §1-11. These record a dated measurement (2026-09-03/04, llama.cpp build 10770) and were not judged.

### F5 — CODING_STANDARDS, PROMPT_CORPUS, artifact-builder, the runbooks and `.env.example`

**Documents:** `docs/reference/CODING_STANDARDS.md`, `docs/reference/PROMPT_CORPUS.md`, `docs/reference/artifact-builder/` (SKILL.md and 7 reference files), `docs/runbooks/` (2 files), and the comments in `.env.example`.

**Verdict:** POSITIVE — 39 wrong statements.

**Arms for the negative sub-claims in this table:**

- **Arm 1 (identifier provenance).** Each negative row quotes the document's own class, setting, unit, skill, seat or key name. Deleted items carry their commit (`a5b1729e` for `LocalLLMClient`, `b3754731` for the `prime-worker` skill, `910954fc` for the seat rename). For the four `.env.example` secret names (E25, E26, E28), the script `E_envaudit.py` searched every reader location with `grep -rlI -w <KEY>` (src, config, scripts, docker, infrastructure, Makefile, e2e, the 5 compose files, Dockerfile.gateway, seshat-pwa, plugins, .claude/hooks, skills). The only hits for the `AGENT_` spellings are the allow-lists in `config_guard.py:88-97` and `scripts/audit/config_inventory.py`.
- **Arm 2 (path liveness).** Controls with the same method: the unprefixed `CLOUDFLARE_TUNNEL_TOKEN` → `docker-compose.cloud.yml:574`; `GATEWAY_TOKEN_PWA` → `config/gateway_access.yaml:33`; `MissingIdentityError` → `exceptions.py:13` against 0 for `PersonalAgentError`; the live gateway environment key names (names only, 133 keys, 0 values) contain the unprefixed spellings.
- **Arm 3 (scope).** Repo searches cover `main` at `e5c89ff4`. E16 and E22 cover the VPS systemd unit files on 2026-10-29 (`systemctl list-unit-files 'claude-remote-control*'` → "0 unit files listed"). E20 and E21 cover the live tmux sessions on 2026-10-29.

| # | file:line | says | true | source | kind |
|---|---|---|---|---|---|
| E1 | CODING_STANDARDS.md:363-379 | exceptions.py PersonalAgentError, ConfigurationError, GovernanceError, ModeTransitionError | none exist; exceptions.py holds MissingIdentityError etc. | grep ^class → 0 each; control MissingIdentityError exceptions.py:13 | code-name |
| E2 | CODING_STANDARDS.md:387, 375-376 | from personal_agent.exceptions import ToolExecutionError | in tools/contract.py:58; LLMClientError in llm_client/types.py:187 | as cited | code-name |
| E3 | CODING_STANDARDS.md:320, 407 | PermissionDeniedError, ToolNotFoundError | do not exist | grep → 0 | code-name |
| E4 | CODING_STANDARDS.md:122, 816, 821, 856 | from personal_agent.llm_client import LocalLLMClient | deleted 2026-09-04 (FRE-1367) | git log -S → a5b1729e | code-name |
| E5 | CODING_STANDARDS.md:651, 688, 712, 747, 757, 1057 | settings.llm_base_url | slm_base_url, no default | T4; settings.py:186 | setting |
| E6 | CODING_STANDARDS.md:756, 1055 | settings.api_key | no such field; per-provider keys | settings.py:1633, 1638 | setting |
| E7 | CODING_STANDARDS.md:79 | from personal_agent.governance import PolicyConfig | never existed | git log -S → none | code-name |
| E8 | CODING_STANDARDS.md:727 | from personal_agent.governance import load_governance_config | deprecated, warns; canonical personal_agent.config | governance/__init__.py:26 | code-name |
| E9 | CODING_STANDARDS.md:805-810 | TraceContext(trace_id=…, span_id=…) | no span_id field → TypeError | telemetry/trace.py:119-124 | code-name |
| E10 | CODING_STANDARDS.md:769-773 | tests/test_telemetry/ | tests/personal_agent/telemetry | ls → missing | path |
| E11 | CODING_STANDARDS.md:24 | see project AGENTS.md | no root AGENTS.md, never | git log → empty | path |
| E12 | CODING_STANDARDS.md:36 | we control the environment (local Mac) | Debian VPS | .claude/CLAUDE.md; T2 | other |
| E13 | PROMPT_CORPUS.md:3 | Source revision 411a8a43 | stale: 22 commits to rendered sources since, newest 4f65dbfe 2026-10-25; e.g. :1016 sentence removed from skills/log-search/SKILL.md; :30 reflection.py:172 now :294 | scripts/render_prompt_corpus.py:156-293; git log 411a8a43..HEAD | other |
| E14 | artifact-builder/references/{chartjs,highlightjs,katex,ofl-fonts,pagedjs,threejs}.md:3; maintenance.md:1 | Part of the artifact-design skill | no such skill; FRE-1590 moved it out; SKILL.md:5 "not an agent skill" | ls skills/artifact-design → missing; 773c4c32 | path |
| E15 | artifact-builder/references/maintenance.md:29 | KaTeX 0.16.11 | fixture builder pins 0.16.47 | scripts/build_e2e_artifact_fixtures.py:66-67; config/artifact_lib_manifest.json | other |
| E16 | runbooks/dispatch-orchestrator.md:49-56, 126, 130-133 | systemctl enable claude-remote-control@build1 | units not installed | live systemctl list-unit-files → 0 | service |
| E17 | dispatch-orchestrator.md:89-91, 681-683 | check-pytest-lock hook stays live | removed 2026-07-18 (runbook :187 says so) | CLAUDE.md; .claude/settings.json, hooks → 0 | feature |
| E18 | dispatch-orchestrator.md:267-276 | bounce/red CI → send-keys "/prime-worker" | watcher sends plain text "PR #N failed CI checks"; bounce leg removed | gating_watcher.py:768; feab4a04 2026-07-09; BOUNCE count 0 | feature |
| E19 | dispatch-orchestrator.md:242-252 | prime-worker skill is a PR-feedback monitor | deleted 2026-07-09, folded into build | ls → missing; b3754731 | path |
| E20 | dispatch-orchestrator.md:275, 638-640, 676 | cc-build/cc-build2/cc-adrs | cc-1build, cc-2build (FRE-909, 2026-07-18) | launcher.py:304-323; 910954fc; live tmux ls | service |
| E21 | dispatch-orchestrator.md:146, 184, 211, 273 | tmux attach -t cc-<stream> | yields cc-build1 etc., which do not exist | orchestrator.py:99; launcher.py:304-323; live tmux ls | command |
| E22 | dispatch-orchestrator.md:665-692 | phased cutover remains (boxes unchecked) | Phase C done: orchestrator enabled, --loop --execute | live systemctl cat seshat-dispatch-orchestrator | service |
| E23 | dispatch-orchestrator.md:658-660 | only kick-off skills = entire allowlist | allowlist also grants Bash, Read, Grep, Glob, Linear reads | .claude/settings.json:16-30 | other |
| E24 | runbooks/embedder-managed-adoption.md:1, 23-27, 57-60 | a same-model local instance becomes the failover | adoption done another way: OVH Qwen3-Embedding-8B everywhere; no fallback endpoint; runbook self-flags stale :14-21 | T5,T6; live env key names | feature |
| E25 | .env.example:48, 803 | AGENT_CLOUDFLARE_TUNNEL_TOKEN | compose reads CLOUDFLARE_TUNNEL_TOKEN | docker-compose.cloud.yml:574; live key names; AGENT_ name only in allow-lists config_guard.py:95 | setting |
| E26 | .env.example:92-93, 365-366, 804 | AGENT_GATEWAY_TOKEN_PWA, AGENT_GATEWAY_TOKEN_EXTERNAL_AGENT | readers use GATEWAY_TOKEN_PWA / _EXTERNAL_AGENT | config/gateway_access.yaml:33, 43; compose:446 | setting |
| E27 | .env.example:363 | values must match hashed secrets in gateway_access.yaml | not hashed; ${ENV} raw token, hmac.compare_digest | gateway_access.yaml:33, 43; gateway/auth.py:201 | other |
| E28 | .env.example:812 | AGENT_MCP_SECRETS_FILE | MCP_SECRETS_FILE | compose:348; docker/mcp/run-gateway.sh:8 | setting |
| E29 | .env.example:837-839 | build-time NEXT_PUBLIC_SESHAT_URL | PWA reads SESHAT_URL at runtime (FRE-339) | seshat-pwa/src/app/layout.tsx:68-76; compose:436 | setting |
| E30 | .env.example:799 | required secrets — compose fails loudly if unset | only POSTGRES_PASSWORD, NEO4J_PASSWORD use :? | compose:52, 96, 446, 492-493, 574 | other |
| E31 | .env.example:176-178 | no-think suffix Default: true | False | settings.py:210-211 | setting |
| E32 | .env.example:187-189 | task timeout Default: 300 | 900 | settings.py:225-226 | setting |
| E33 | .env.example:259-261 | context token budget Default: 2048 | 48000 | settings.py:1234-1235 | setting |
| E34 | .env.example:416-418 | 768 for Qwen3-Embedding-0.6B, Default 768 | 1024; embedder OVH Qwen3-Embedding-8B; contradicts own :476-477 | settings.py:735-736; T5 | setting |
| E35 | .env.example:424-426 | dedup Default 0.85 | 0.92 | settings.py:748-749 | setting |
| E36 | .env.example:495-498, 268, 371-378 | process-to-model assignments in config/models.yaml: entity_extraction_role | config/model_roles.yaml bindings; no *_role keys | model_roles.yaml:1-11, 94, 123; T6 | path |
| E37 | .env.example:511-512 | tunnelled MLX | llama.cpp | T4 | model |
| E38 | .env.example:785, 790-791 | required for make eval: PERSONAL_AGENT_EVAL=1 | make eval sets it itself | Makefile:254-255 | command |
| E39 | .env.example:55 | cd infrastructure/terraform-cloudflare-mac && terraform apply | moved to private companion repo 2026-05-01 | ls → missing; ede28405 | path |

**Unconfirmed (not counted):**

- `.env.example:244-250`, `AGENT_ROUTER_ROLE` and `AGENT_ENABLE_REASONING_ROLE`. The fields exist and the defaults match. The study did not trace whether any current code path uses them.
- `.env.example:295-297`, GPU monitoring through `powermetrics`. The behavior on Debian was not checked.
- `.env.example:459`, the substrate profile list. It leaves out `managed_embedder`. That is an omission, not a wrong statement.
- `.env.example:461`, "Default 'private' is today's behavior". The live value cannot be read without a `.env` value.
- `PROMPT_CORPUS.md`, which blocks changed. The study did not run the renderer.

## Verdict per document (AC-2)

`Rows` is the number of wrong statements. `Fix` is the kind of change that each document needs: line fixes, a rewrite, a regeneration, or a move to the archive.

| Document | Verdict | Rows | Fix |
|---|---|---|---|
| `README.md` | has wrong statements | 16 | **rewrite** — it describes the 2026-03 single-model, local-only design and omits the PWA, the cloud stack and the role bindings |
| `CLAUDE.md` | has wrong statements | 7 | line fixes (model line, request-flow diagram, key settings, extraction model, CLI and test-file examples, `ENV=cloud make ps`) |
| `.claude/CLAUDE.md` | has wrong statements | 3 | line fixes |
| `tests/CLAUDE.md` | current | 0 | — |
| `docs/README.md` | has wrong statements | 1 | line fix (one broken link) |
| `docs/VISION_DOC.md` | has wrong statements | 1 | line fix |
| `make help` (Makefile:66-108) | has wrong statements | 1 | line fix |
| `docs/specs/COGNITIVE_ARCHITECTURE_REDESIGN_v2.md` | has wrong statements | 25 | **rewrite** — the core (7 stages, TaskType, 4 strategies) holds, but Stages 1, 2, 3 and 7, skills, the memory protocol, the loop gate, model config, Kibana and LM Studio follow a superseded design, and the status header claims items that never shipped |
| `docs/specs/SESHAT_PROGRAM_ARCHITECTURE.md` | has wrong statements | 4 | line fixes, plus an "as of" date on §§7-8 |
| `docs/guides/AGENTS.md` | current | 0 | — (edit its index when P2 moves files) |
| `docs/guides/cursor-hooks.md` | current | 0 | — |
| `docs/guides/MACMON_GPU_METRICS.md` | current | 0 | — |
| `docs/guides/LINEAR_FEEDBACK_LOOP.md` | current | 0 | — |
| `docs/guides/MCP_INTEGRATION_QUICK_START.md` | has wrong statements | 1 | line fix |
| `docs/guides/MAC_OTEL_COLLECTOR.md` | has wrong statements | 1 | line fix |
| `docs/guides/GPU_METRICS_SECURITY.md` | has wrong statements | 1 | line fix |
| `docs/guides/GPU_METRICS_SETUP.md` | has wrong statements | 3 | line fixes |
| `docs/guides/TELEMETRY_ELASTICSEARCH_INTEGRATION.md` | has wrong statements | 4 | line fixes |
| `docs/guides/CLOUD_DEPLOYMENT.md` | has wrong statements | 10 | line fixes, and replace §10 (profiles) |
| `docs/guides/CONFIGURATION.md` | has wrong statements | 9 | **rewrite** — LM Studio quick start, un-prefixed setting names, wrong defaults |
| `docs/guides/USAGE_GUIDE.md` | has wrong statements | 9 | **rewrite** — CLI form, response shape, profiles, model server, router model |
| `docs/guides/AGUI_STREAMING_REFERENCE.md` | has wrong statements | 8 | **rewrite** — describes SSE `/stream/*`; the transport is WebSocket `/ws/*` |
| `docs/guides/SCHEMA_REFERENCE.md` | has wrong statements | 17 | **rewrite** — API, transport, config, Terraform and Postgres sections describe removed designs |
| `docs/guides/SLM_SERVER_INTEGRATION.md` | obsolete | 1 (whole file) | **archive** |
| `docs/guides/EVALUATION_PHASE_GUIDE.md` | obsolete | 1 (whole file) | **archive** |
| `docs/guides/IMPLEMENTATION_QUICK_REFERENCE.md` | obsolete | 1 (whole file) | **archive** |
| `docs/guides/METRICS_STORAGE_GUIDE.md` | obsolete | 1 (whole file) | **archive** |
| `docs/reference/PATH_PRIVACY.md` | current | 0 | — |
| `docs/reference/PROMPT_DEBUG_CAPTURE.md` | current | 0 | — |
| `docs/reference/PR_REVIEW_RUBRIC.md` | current | 0 | — |
| `docs/reference/CAPTAINS_LOG_HUMAN_APPROVAL_INVARIANT.md` | has wrong statements | 2 | line fixes (line citations) |
| `docs/reference/DEPENDENCY_SECURITY.md` | has wrong statements | 1 | line fix |
| `docs/reference/POSTGRES_SCHEMA_DEBT_AUDIT.md` | has wrong statements | 1 | line fix (an audit record for FRE-597) |
| `docs/reference/DATA_LIFECYCLE.md` | has wrong statements | 3 | line fixes |
| `docs/reference/DELIVERY_PIPELINE.md` | has wrong statements | 1 | line fix |
| `docs/reference/SLM_SERVER_CLIENT_SEMANTICS.md` | has wrong statements | 2 | line fixes |
| `docs/reference/TOOL_INTEGRATION_GUIDE.md` | has wrong statements | 2 | line fixes |
| `docs/reference/VALIDATION_CHECKLIST.md` | has wrong statements | 3 | line fixes |
| `docs/reference/AGENT_WORKFLOW_METHODOLOGY.md` | has wrong statements | 1 | line fix |
| `docs/reference/ROOT_LEVEL_POLICY.md` | has wrong statements | 1 | **rewrite** — its lists of root entries do not match the repo |
| `docs/reference/PROJECT_DIRECTORY_STRUCTURE.md` | has wrong statements | 5 | **rewrite** — tree, directory list and `.gitignore` block do not match |
| `docs/reference/CONFIG_INVENTORY.md` | has wrong statements | 11 | **regenerate** §1, and date or rewrite §0 and §2-§8 (a 2026-07-03 audit presented as current) |
| `docs/reference/ENTITY_EXTRACTION_MODELS.md` | obsolete | 6 | **archive** |
| `docs/reference/TESTING_STANDARDS.md` | obsolete | 4 | **archive** (`tests/CLAUDE.md` holds the current test policy) |
| `docs/reference/AGENT_MD_STRATEGY.md` | obsolete | 2 | **archive** |
| `docs/reference/CODING_STANDARDS.md` | has wrong statements | 12 | **rewrite** of the code examples (the rules stand) |
| `docs/reference/PROMPT_CORPUS.md` | has wrong statements | 1 | **regenerate** (`make render-prompt-corpus`) |
| `docs/reference/artifact-builder/SKILL.md` | current | 0 | — |
| `docs/reference/artifact-builder/references/chartjs.md` | has wrong statements | E14 | line fix (line 3) |
| `docs/reference/artifact-builder/references/highlightjs.md` | has wrong statements | E14 | line fix (line 3) |
| `docs/reference/artifact-builder/references/katex.md` | has wrong statements | E14 | line fix (line 3) |
| `docs/reference/artifact-builder/references/ofl-fonts.md` | has wrong statements | E14 | line fix (line 3) |
| `docs/reference/artifact-builder/references/pagedjs.md` | has wrong statements | E14 | line fix (line 3) |
| `docs/reference/artifact-builder/references/threejs.md` | has wrong statements | E14 | line fix (line 3) |
| `docs/reference/artifact-builder/references/maintenance.md` | has wrong statements | E14, E15 | line fixes |
| `docs/runbooks/dispatch-orchestrator.md` | has wrong statements | 8 | **rewrite** — RC units, the removed hook, the deleted skill, seat names and the cutover checklist |
| `docs/runbooks/embedder-managed-adoption.md` | obsolete | 1 | **archive** (it marks itself stale) |
| `.env.example` (comments) | has wrong statements | 15 | line fixes; the four secret names first |

Totals: 58 documents. 9 current, 41 with wrong statements (12 of them need a rewrite or a regeneration), 8 obsolete.

## Proposals

### P1 — Fix in five stages, in this order (AC-3)

The order follows how many readers a wrong fact reaches and how much harm it does.

1. **Stage 1 — the shared stale facts in the entry documents, in one pass.** Fix `CLAUDE.md`, `.claude/CLAUDE.md` and `make help`, and rewrite `README.md`. Every session loads the two CLAUDE.md files. The README is the owner's entry point. All four repeat the same five stale facts (model, model server, setting, CLI, removed designs). One pass keeps the wording the same everywhere.
2. **Stage 2 — the documents that agent-loaded files cite as the reference.**
   - Rewrite `COGNITIVE_ARCHITECTURE_REDESIGN_v2.md`. `CLAUDE.md`, `.claude/CLAUDE.md` and `src/personal_agent/orchestrator/AGENTS.md` cite it.
   - Fix `TOOL_INTEGRATION_GUIDE.md`. `CLAUDE.md`, `.claude/CLAUDE.md` and `src/personal_agent/tools/AGENTS.md` cite it.
   - Fix `SESHAT_PROGRAM_ARCHITECTURE.md`. `CLAUDE.md` cites it.
   - Rewrite the examples in `CODING_STANDARDS.md` and rewrite `PROJECT_DIRECTORY_STRUCTURE.md`. `src/personal_agent/AGENTS.md` cites both.
   - Rewrite `docs/runbooks/dispatch-orchestrator.md`. The `prime-master` skill cites it.
3. **Stage 3 — the operator documents.** Fix `.env.example`, with the secret names first (E25, E26, E28, then E29). An operator who follows the file today sets a name that no code reads. Then fix the wrong defaults (E31-E35) and `CLOUD_DEPLOYMENT.md`.
4. **Stage 4 — archive and regenerate.** Move the 8 obsolete documents (P2). Run `make render-prompt-corpus`. Regenerate §1 of `CONFIG_INVENTORY.md`, and give §0 and §2-§8 a dated audit header or rewrite them.
5. **Stage 5 — the rest.** Rewrite `CONFIGURATION.md`, `USAGE_GUIDE.md`, `AGUI_STREAMING_REFERENCE.md`, `SCHEMA_REFERENCE.md` and `ROOT_LEVEL_POLICY.md`. Then make the line fixes in the other 22 documents.

**Documents that need a rewrite instead of line fixes (12):** `README.md`, `COGNITIVE_ARCHITECTURE_REDESIGN_v2.md`, `CODING_STANDARDS.md` (examples only), `PROJECT_DIRECTORY_STRUCTURE.md`, `ROOT_LEVEL_POLICY.md`, `dispatch-orchestrator.md`, `CONFIGURATION.md`, `USAGE_GUIDE.md`, `AGUI_STREAMING_REFERENCE.md`, `SCHEMA_REFERENCE.md`, and the two regenerations, `CONFIG_INVENTORY.md` and `PROMPT_CORPUS.md`.

Step 2 of FRE-1703 is one build ticket. 209 rows in one PR are hard to review. Each stage is a natural PR boundary.

### P2 — Move the 8 obsolete documents to `docs/archive/`

The 8 documents are `SLM_SERVER_INTEGRATION.md`, `EVALUATION_PHASE_GUIDE.md`, `IMPLEMENTATION_QUICK_REFERENCE.md`, `METRICS_STORAGE_GUIDE.md`, `ENTITY_EXTRACTION_MODELS.md`, `TESTING_STANDARDS.md`, `AGENT_MD_STRATEGY.md` and `docs/runbooks/embedder-managed-adoption.md`. Each one describes a design or a plan that no longer exists. A line fix leaves a document whose purpose is gone.

Live files outside the dated records refer to them 35 times. The counts are SLM_SERVER_INTEGRATION 11, EVALUATION_PHASE_GUIDE 6, METRICS_STORAGE_GUIDE 5, IMPLEMENTATION_QUICK_REFERENCE 4, ENTITY_EXTRACTION_MODELS 3, embedder-managed-adoption 3, AGENT_MD_STRATEGY 2 and TESTING_STANDARDS 1. Measured with `grep -rlF <name>` over `*.md`, `*.py` and `*.yaml`, without `docs/archive`, `docs/research`, `docs/plans` and `docs/architecture_decisions`. The move must update those references, `docs/README.md` and `docs/guides/AGENTS.md`.

### P3 — Repair the docs link checker and run it in CI (filed as FRE-1704)

`scripts/verify_docs_links.py` exists, and nothing calls it. On `main` it crashes before it reports: `python3 -I scripts/verify_docs_links.py --skip-external` → rc=1, `ValueError … is not in the subpath of …/docs` at line 225. It also checks only Markdown links under `docs/`. Of the 209 rows, 47 are wrong paths, 10 are wrong commands and 31 are wrong setting names or defaults. A mechanical check finds most stale paths, make targets and `AGENT_*` names, so it slows the return of this drift. It does not find wrong models, features or counts. Those need a periodic audit like this one.

## Filed tickets

- **FRE-1704** — The docs link checker crashes on main and nothing runs it. Repair it and check cited repo paths, make targets and setting names (Backlog, related to FRE-1703).

## Appendix — method

**What was measured, and where.**
- **Code and config:** `main` at `e5c89ff4`, read in a detached scratch worktree. The deployed files match by SHA-256 (T1).
- **Live stack:** `docker ps`; files and environment key names (names only) in the production gateway container; production Elasticsearch (`_count`, `_search`, `_cat/indices`, `_ilm/policy`); production Postgres (read-only `SELECT`, `\dt`, `\d`); the Tempo HTTP search API; VPS systemd unit files; the tmux session list; and the Linear labels of the FrenchForest team (read only). All checks ran on 2026-10-29.

**How the work was split.** cc-explore measured T1-T8 first and wrote them to a truth sheet. Five reviewers then checked one document group each against the truth sheet and the tree:
- A: the entry documents (F1);
- B: the two specs (F2);
- C: `docs/guides/` (F3);
- D: `docs/reference/` without three items (F4);
- E: CODING_STANDARDS, PROMPT_CORPUS, artifact-builder, the runbooks and `.env.example` (F5).

Each reviewer returned one row per wrong statement, with a source, and for each negative a search plus a control. Each reviewer also returned an "Unconfirmed" list and one verdict per document.

**Verification by cc-explore.** cc-explore ran 17 rows again from all five groups: A3, A8, A22, B3, B10, C16, C29, C38, C62, D7, D8, D25, D36, E16, E25, E31 and E33. All 17 held. During the check of B3 and B10, cc-explore found X1 in `CLAUDE.md`, which reviewer A had missed.

**Secrets.** No `.env` file was opened. Environment checks printed key names only. The key-name file of reviewer E holds 133 names and 0 `=` characters.

**One incident.** An early import check by reviewer E created `.venv/` and `__pycache__/` directories in the scratch checkout. The reviewer removed them, and `git status` was clean afterwards. The study made no change to the repository or to the live system.

**Not measured.**
- No gateway HTTP call, no PWA page load, no model call, no test run. CLI behavior (A8, C20) comes from the CLI source (`service_cli.py:142-153`), plus a standalone typer app of the same shape.
- The owner's Mac and the separate `slm_server` repository cannot be seen from the VPS. Claims about them are in the "Unconfirmed" lists.

**Rejected.**
- A line-by-line audit of `PROMPT_CORPUS.md`. The file is generated, so the check measured how stale it is: 22 commits since `411a8a43` touch its sources, the newest `4f65dbfe` (2026-10-25).
- Rows against dated records (T8).
- Omissions as rows, for example a missing provider in a table. A row needs a false statement.

**Identifier resolutions.**
- `LLM_BASE_URL` → `slm_base_url` / `AGENT_SLM_BASE_URL` (ADR-0132 D4).
- `agent "<message>"` → `agent chat "<message>"`.
- SSE `/stream/{session_id}` → WebSocket `/ws/{session_id}`.
- `*_role` keys in `models.yaml` → the `bindings:` in `config/model_roles.yaml`.
- `api_calls` → `api_costs`.
- `LocalLLMClient` → `LiteLLMClient`.
- The seat names `cc-build`, `cc-build2` → `cc-1build`, `cc-2build`.
- `AGENT_CLOUDFLARE_TUNNEL_TOKEN`, `AGENT_GATEWAY_TOKEN_*`, `AGENT_MCP_SECRETS_FILE` → the unprefixed names that compose and `gateway_access.yaml` read.
