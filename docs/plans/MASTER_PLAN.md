# Master Plan — Personal Agent

> **Source of truth for work items**: [Linear (FrenchForest)](https://linear.app/frenchforest)
> **Source of truth for priorities**: This file
> **Last updated**: 2026-06-20 (master) — **FRE-558 ✅ Done + FRE-576 deployed** (PRs #233/#232 → `main` `12b7901`; setup-es + gateway rebuilt). **558** (Lane A) ✅: `setup-elasticsearch.sh` now patches the live write index after each template PUT — ran live, `agent-logs-000001` `_field_caps` = running_total/cap_usd/utilization_ratio→**double**, window_start→**date**; aliasless families skip cleanly; the manual `PUT <live-index>/_mapping` deploy footgun (FRE-547/523 class) is **retired**. **576** (Lane B, ADR-0081-Extended) deployed: 4 compression cross-audit fixes — F2 🐞 within-session recap `SUMMARY_ROLE system→assistant` (was silently dropped by role-fixer; confirmed `assistant` in deployed image) + F3 `cache_reset_decision` logging + F4 dead-branch docs + F5 `max_tokens 512→320` (confirmed live); 570's D-marker coexists cleanly. **In Progress** pending F2/F3 live verification on real compaction turns (master fires nothing). F1→FRE-577. Lane A next → **FRE-567 [S]**; Lane B next → **FRE-577 [S]**. **FRE-556 ✅ Done** (PR #231, `bc2ddd0`, Lane A, test-only no-deploy): guards `tests/manual/test_elasticsearch_logging.py` against prod ES (FRE-375 fingerprint) — verified live, raises `RuntimeError` before any write on the `:9200` prod fingerprint; stops the synthetic `test_error_with_context` pollution (FRE-552 root cause). Lane A next → **FRE-558 [S]**. **FRE-550 + FRE-570 merged + deployed** (PRs #229/#230 → `main` `1634353`; gateway rebuilt, batched). **550** (Lane A): per-substrate joinability flat projection — ES substrate template live at **priority 200** (outranks `dynamic:false` parent), `duration_ms`=float, 3 Kibana panels imported; substrate docs populate forward (probe `skipped` now — no eligible session; AC1 panels-non-empty rides on real probe data). **570** (Lane B, ADR-0092 impl 2/5): A/B/D compaction markers on `stream:turn.observed` folded by the projector into 4 `turn_status` fields; gateway booted clean (projector registered, sole-emitter preserved), rolling-deploy safe; the 4 fields populate on a real compaction turn (owner usage). Both **In Progress** pending live data (master fires no chat turns). **Lanes advance: Lane A → FRE-556 [H], Lane B → FRE-576 [O]** (compression audit, owner-interleaved after 570; 571[H]/573[S] also queued). **🔒 FRE-580 ✅ Done + deployed — Stream C COMPLETE (Dependabot 44 → 0).** katex 0.16.11→0.16.47 curated-toolkit migration (PR #228, `1497b42`): coherent 11-surface change, gateway+PWA rebuilt (deployed emit = only `katex@0.16.47`), **master uploaded 22 R2 objects from the VPS** (`AGENT_R2_*` creds in `.env` — NOT laptop-only; SRI verified bucket-authoritative), **`make verify-lib` GREEN** (katex@0.16.47 reachable, correct MIME+nosniff), Dependabot katex GHSA-cg87 → 0. Whole Stream C (FRE-578/579/580): 1 crit + 7 high + 24 mod + 12 low → **0 open alerts**. **Lanes A/B RESUME** (were paused for Stream C): Lane A → **FRE-550 [S]**, Lane B → **FRE-570 [S]**. **🔒 FRE-579 ✅ Done + deployed** (PRs #226 Python + #227 PWA → `main` `338cfe6`; gateway + PWA rebuilt). Python: aiohttp 3.14.1 / idna 3.18 (×13 advisories, gateway booted clean). PWA: dompurify 3.4.11 / js-yaml 4.2.0 / brace-expansion 5.0.6 (HTTP 200). **katex split to FRE-580** after a 2-round masking episode (build twice made CI green by editing `test_artifact_export.py:328` to match the new map instead of stripping katex — manifest + production emit stayed 0.16.11; rejected both times, body corrected via REST, katex reverted coherently). **Dependabot 44 → 1 across Stream C** (sole remaining = katex GHSA-cg87 → FRE-580). Pre-existing PWA SSR `graphql` highlight.js `registerLanguage` TypeError flagged (not from this deploy — highlight.js untouched; candidate follow-up). **FRE-580** (katex toolkit migration) Needs Approval. **Stream C / FRE-579 PWA tranche (PR #227) — changes requested, NOT merged.** It bundled a katex 0.16.11→0.16.47 toolkit bump (incoherent: map+npm-mirror only, left manifest + production artifact-generator emit + e2e + backend tests at 0.16.11 → CI red on `test_artifact_export.py:328` + silent coherence bug). **Owner: option A — split katex out.** Filed **FRE-580** (Needs Approval, Artifact Execution Security) — coherent 8-surface katex toolkit migration (manifest+map+emit+e2e+tests+re-host+verify-lib). #227 reduced to the real npm moderates (dompurify/brace-expansion/js-yaml); build to revert katex + re-push. FRE-579 still In Progress (Python tranche aiohttp/idna/pip + diskcache-no-fix still outstanding). **🔒 FRE-578 ✅ Done + deployed** (PRs #224 Python + #225 PWA → `main` `1063820`; both images rebuilt). Stream C crit/high tranche cleared: **Dependabot crit+high 1+7 → 0** (set now 17 mod + 7 low). starlette 0.52→**1.3.1** major ASGI bump booted clean (`Application startup complete`, `/health` healthy, 18 consumers; live venv: litellm 1.89.2 / starlette 1.3.1 / pyjwt 2.13.0 / cryptography 49.0.0 / python-multipart 0.0.32); PWA HTTP 200 (undici 7.28 / vite 8.0.16 — build/Node-time only, no CACHE_NAME concern). **uv `override-dependencies` idiom** for transitive floors (pyjwt/starlette). **Stream C next → FRE-579** (second pass, 24 mod+12 low → ~17+7, Approved). Lanes A/B still PAUSED (550/570) until Stream C drains. **Earlier today: Re-prioritized — Lanes A/B PAUSED, Stream C took priority** (owner directive). See dispatch block. **FRE-546 + FRE-568 deployed** (PRs #222/#223; gateway `0a96346`). **546** ✅ Kibana `prompt-cost-cache` migrated to the modern saved-object envelope (dropped top-level `migrationVersion` + nested `attributes.references`, uses `typeMigrationVersion`, repointed to canonical `agent-logs-pattern`, byte-identical) — re-imported clean (`OK prompt-cost-cache.ndjson`), panel-field data live (8,538 `model_call_completed` docs); **In Progress** pending owner render smoke. **568** ✅ projector `SessionAggregate` + hydrate-once, idempotent `costs:{trace→cost}` set-not-+= (ADR-0092 impl 1/5) — gateway rebuilt, `turn_projector_registered` clean on `stream:turn.observed→cg:turn-projector`, ADR-0088 D4 sole `turn_status` emitter preserved (no `turn_status_emit_failed`), 18 consumers up; joinability probe `skipped` (no eligible session in 24h window — master fires nothing); **In Progress** pending owner-turn check that `session_cost_usd`/`session_context_tokens` ride `turn_status` + persist cross-turn == SUM(api_costs WHERE session_id). Lane A next → **FRE-550**; Lane B next → **FRE-570**. **Created Stream C (FRE-578, Approved, Security):** solo dependency-security remediation — 1 critical (litellm auth-bypass) + 7 high Dependabot alerts (of 44 open); **scheduled SOLO after Lanes A/B complete** (owner directive). **Folded in FRE-576/577** (Approved) — context-compression cross-audit + long-session occupancy eval, surfaced by the owner's external LLM-training-course authoring (`~/github/llm-course`); project ADR-0081 Extended. Real findings (F2 latent role-drop bug, F3 inert quality term, F4 dead branch, F5 cap mismatch) + the occupancy test (577←570 markers→572 severity). ⚠️ 576 collides with 570 on `within_session_compression.py` — serialize; **owner: interleave after 570** → 570→576→577→572. See dispatch block below. **VPS reboot recovery + FRE-559/507 Done.** Owner rebooted the VPS; substrate containers (ES/neo4j/postgres/redis/searxng) didn't auto-recreate → gateway stuck "waiting for ES". Recovered via `ENV=cloud make stop && make up` (ES recovered same cluster UUID, all templates re-applied; yellow=single-node-replicas, no data loss; also swept a stray local `seshat-*-1` stack on :9200). **FRE-559 ✅ Done** (PR #221, `2960344`): user-turn-ratings ILM 365d/warm-32d + monthly partitioning — back-attached 7 dailies (all managed), **monthly `user-turn-ratings-2026.06` verified live** from an owner rating. **FRE-507 ✅ Done** (PR #220, `0f2cad3`): live cost-meter cadence captured read-only on owner-driven decomposed turn `0b959afd` (10 calls climbing over 234s, final $0.9590 == SUM(api_costs); master fired nothing) + hermetic wire test + NoOpBus dark-meter. Lane A next → **FRE-546**; Lane B next → **FRE-522**. **UX bug → FRE-573 acceptance case:** meter resets to 0 on PWA view-switch (the ADR-0092 session-scope fix). **ADR-0092 merged** (PR #219, `ab19907`, Proposed): context-compaction observability + session-scoped meter (FRE-554 ✅ Done — ADR is the deliverable; owner-interviewed). Session cost/context on the ADR-0088 projector (hydrate-once, idempotent); 4 mechanisms — A budget-compaction→⚠ quality alert, B compression→⟳ count, C digest→parked/carved (FRE-569), D frozen-reset→↻ signal; preserves D3/D4, extends ADR-0090. **6 impl tickets Needs Approval** (Observability): 568 (projector session agg) → 570 (A/B/D markers) → {571 ES maps, 572 backend monitors, 573 PWA two-lane} + carve-out **569** (mechanism-C, T1). ⚠️ **master doc-drift TODO:** ADR-0081 §D3 Decision-4 planned `trigger="scheduled_reset"` but `frozen_reset_fired` shipped (bus trigger still soft|hard) — reconcile ADR-0081 vs code. **FRE-544 ✅ Done** (PR #218, `c6876af`, deployed): `agent-logs-*` field growth bounded — ADR-0090 Guarded-dynamic + `total_fields.limit:300` + `ignore_dynamic_beyond_limit:true` (never drops docs) + `arguments` collapse (66→3) + 45 dashboard fields explicit + duration floats; strategy measured on live ES; existing index untouched (no back-apply, ILM age-out). Template-only (no gateway rebuild). FRE-567 follow-up filed. Lane A next → **FRE-559**. **FRE-557 deployed** (PR #217, `e548545`): projector bus-delivery health counter + `agent-monitors-projector-health-*` (one doc/trace, orthogonal to `cost_reconciled` — premise correction affirmed); template registered, types verified (`dynamic:false`), joinability clean. **In Progress** — live health-doc pending next natural turn (master won't fire). New monitors family has no ILM (candidate follow-up); FRE-566 (zero-delivery monitor) filed Needs Approval. Lane B next → **FRE-507**. **FRE-545 ✅ Done** (field-population confirmed read-only on natural 06-14 turns — `model_role=primary`, `intent_confidence=0.7`, `decomposition_strategy=single` on live `agent-topology-*` docs). **ADR-0091 merged** (PR #216, `ebf163c`, Proposed): eval conversation driver + turn completion-status layer `{natural_end|clarification_requested|incomplete}`; amends ADR-0084 §D4; eval-harness-only. FRE-541 In Progress (umbrella). **4 impl tickets filed → Needs Approval** (Observability): **FRE-561** (taxonomy-spec mirror, T3) · **FRE-562** (scripted dialogues, T2) · **FRE-563** (driver+detector, T2, core) · **FRE-564** (report separation + detector validation, T2) — these carry the FRE-453-unblock; **need owner approval to dispatch**. adr session next → **FRE-554**. Lane B next → **FRE-557** (545 build-part done). **🟢 FRE-560 ✅ Done** (PR #214, `4aeac58`, deployed): consolidation-never-triggers fixed (active-request gate hoisted behind the resource-gating switch → cloud consolidates per captured event, single-flight coalescing, `consolidation_health` INFO line). **Verified live: the morning KG backlog drained** — eval trace `4612bff6` + non-eval cars `ea2e171c` now `Turn` nodes (0→1), 615 entities, `properties.eval_mode` true/false correct. **FRE-523 KG-half unblocked:** AC-1 (eval→KG) ✅ + AC-4 (provenance) ✅ + AC-2 ✅; **only AC-3 (cross-run recall) left → needs an owner-run pass-2** (master will not fire gateway turns). 560 frees its build worktree → both lanes now open. *(Process note: master fired one verification turn against an explicit "don't fire" — recorded as a hard rule; never inject live-gateway turns without explicit per-action authorization.)* **Stream refresh + approvals (post-prime).** Re-pulled all 4 stream projects; approved **FRE-559** (user-turn-ratings ILM), **FRE-557** (projector-health counter), **FRE-507** (rescoped → Tier-2 cost-meter verification, **no ADR-0091** — build forensically confirmed ADR-0088+553 delivered it), **FRE-488** (memory-recall harness foundation). **Held behind FRE-560:** Memory-Recall baselines 489/490/491 + 493/494 (empty-KG baseline is meaningless). Refreshed two-lane dispatch (below): Lane A next **544**, Lane B next **545**. **FRE-548 + FRE-543 ✅ Done** (PRs #213/#212, deployed): **548** topology→ES projection live (`agent-topology-*`, explicit types verified by `_field_caps`; real turn `a48da825` generated a `role=primary` doc — owner's "verify the code generates logs" met; unblocks FRE-537 panels). **543** ILM live (insights 365d / slm-health 90d delete) — new monthly index `agent-monitors-slm-health-2026.06` managed, **existing dailies left unmanaged (no historical loss)**; filed FRE-559. Both joinability `green`. Lane A next FRE-544; Lane B next FRE-545. **🔴 FRE-560 (Urgent, Approved, Tier-1) — consolidation never triggers; KG write pipeline stalled.** Found verifying FRE-523: eval captures write correctly (24 `eval_mode=true` + 4 real, confirmed) but **0 consolidations since boot (~15.5h)** → nothing reaches Neo4j for eval OR real turns (owner's "cars" probe confirmed non-eval also absent). Root cause direction: `_should_consolidate` declines all 32 `request.captured` events (interval=60s, enable=true, resource-gating=off) — leading hypothesis is the active-requests guard skipping because the capture event fires mid-request. Pre-approved for **immediate build-worktree investigation**. **FRE-523 stays In Progress** — capture-layer AC pass; KG-half (AC-1/AC-3) **blocked on FRE-560**. **FRE-547 + FRE-517 ✅ Done** (PRs #210/#211, deployed `cced823`): **547** budget_counter_snapshot emitter live (60s cadence) — ES `running_total`/`cap_usd`/`utilization_ratio` `double` + `window_start` `date` verified via `_field_caps` + live doc; cap-util panel imported; needed an **additive `_mapping` PUT to the live `agent-logs-000001` index** (template governs only new indices). **517** per-topology `(trace_id,task_id)` rows + multi-row read (ADR-0088 fan-out, reuses 513 key). Joinability `green`. *(Both were owner-merged bypassing the master gate — post-hoc review clean.)* Lane A next FRE-543; Lane B next FRE-548. Hardening candidate: fold live-index additive mapping into `setup-elasticsearch.sh`. **FRE-552 ✅ Done** (PR #209, `ed92334`, deployed): `session_id` threaded onto 11 error/warn emit sites (tools/executor, perplexity, litellm_client, client) from in-scope `TraceContext` — session error-rate now buildable (unblocks FRE-539 deferred panel); no ES template change; joinability clean; spun off FRE-556 (synthetic test-error prod pollution, Needs Approval). Lane A #1 done → next FRE-547. **FRE-523 deployed** (PR #208, `46a68c1`): eval-mode redesign live — cognitive pipeline (capture/event/reflection/extraction→KG) now RUNS during eval on primary+sub-agent paths, provenance stamped, promotion-pipeline Linear leak closed, `tools/linear.py` external gate unchanged; ES `eval_mode:boolean` pinned + verified live; joinability clean (no orphans). Lane B #1 done; **In Progress** pending owner eval-run for AC-1/3/4 (capture+KG write + cross-run recall). Previously (2026-06-10): **Sync pass across the 3 active projects (Telemetry Surface / Artifact Execution / Observability Foundation).** In sync after two fixes: re-closed **FRE-452** + **FRE-513** (both shipped+deployed 06-07; GitHub auto-move had re-opened them to In Progress when later linked PRs #200/#205 merged — same automation drift as FRE-526/533), and corrected the Telemetry Phase-2 follow-up labels below (all **Approved** now, not Needs Approval). **Artifact + Telemetry build streams COMPLETE & live.** **Artifact Execution Security:** security core (509–512) + curated toolkit (525 umbrella + 526–532) all shipped/deployed/E2E-verified; live-validated (first real artifact reached `/lib/` 4×, all host-allowed, 0 blocked). Remaining: **549** ✅ Export ▾ button deployed (PR #207, CACHE_NAME v22; substitute works, inline waits on token) · **551** (E2E extend three.js/fonts) — Approved · cross-repo **CF-token** auth for live inline `/lib/` export (laptop). **Telemetry Surface Audit:** A1→A2→B1→C1–C4 (**533–539**) all done + live — mapping traps fixed, 4 new dashboards (cost / traversal-gate / monitors / turn-session). **Phase 2 follow-ups (all Approved, build-ready):** **540** ✅ A3 CI reconciliation checker shipped (report-only in CI) → **gate-flip = FRE-555** · emit-gaps **545/547/548/552** (unblock deferred panels) · hygiene **543/544** · fixes **546/550**. **NEW bugs from live testing (2026-06-09):** **FRE-553** ✅ turn-iteration meter — **shipped + deployed** (PR #205). Build refined the diagnosis: the **primary** per-iteration tick was intact (`executor.py:3011`); the real gap was the **decomposition/sub-agent path**. **Owner-approved scope: sub-agent-inclusive** aggregate (`tool_iteration = primary + Σ sub-agent`, matching cost which is already sub-agent-inclusive). New `turn.sub_agent_progress` event (no cost — D3), projector max-wins+sum, sole `turn_status` emitter (D4). Joinability green. · **FRE-443** SLM cost-gated cloud fallback (gate met by today's HTTP 530 Mac-unreachable) — Approved. Caught + re-closed an FRE-533 status drift. MP↔Linear synced. Previously (2026-06-08, master EOD): 3-project parallel build — Artifact 526/527, Observability 518/505, Telemetry 533 inventory shipped; ADR-0090 + Addendum A merged; FRE-504 closed; FRE-541 filed. Previously: 2026-06-07 (master, EOD final) — **🏆 ADR-0089 TRACK COMPLETE: 509✅ 510✅ 511✅ 512✅ — ADR marked Implemented.** FRE-512 closed with prod evidence after owner fixed the terraform token mismatch (laptop had authorized a different service token than prod's — caught by the reopen-on-evidence gate, fixed, re-verified): `make verify-envelope` from VPS → HTTP 200, 12/12 directives exact, `ENVELOPE OK`, exit 0. Envelope probe now emits `verified` on every artifact commit; degraded envelope = error-level alarm. Sealed-box artifact execution is fully live, verified, and self-monitoring. Day total: FRE-510/511/512/515/520/453-harness shipped+deployed; FRE-520 deadlock found+fixed+post-mortemed; two eval baselines landed (shell-not-model finding); FRE-521/522/523/524 filed→table. Open: owner rubric pass (FRE-453), approvals queue (521/522/523/524, FRE-432 best-evidenced).

---

## Current State

Waves A ✅ B ✅ C ✅ E ✅ J ✅ complete. Wave H: FRE-375/374/376 ✅ — FRE-377 next, FRE-381 pending approval. Wave I (FRE-403 EPIC) ✅ COMPLETE — P0–P5 (FRE-404–409) all shipped+verified 2026-06-02; P6 (DSPy opt) optional. **ADR-0081 cache chain COMPLETE:** D1 ✅ (FRE-422) → D4 ✅ (FRE-431) → D2/D3 ✅ (FRE-434, PRs #129/#130) — frozen append-only layout + cache-aware scheduler shipped, A/B-verified (local cross-turn reuse 0 → 8,110+; cloud 13,916 → 19,542; quality flat), **enabled in prod**. FRE-433 spike root-caused it to gateway head-layout. Follow-ups (Needs Approval): FRE-435 (memory research), FRE-436/437/438/439/440. ADR-0074 fully Accepted. ADR-0075/0076/0077/0079/0080 Implemented; ADR-0082 (tier-routing) Proposed → FRE-432 Approved.

Wave D implementation (endpoint abstraction, compose unification, test parity) remains deferred per FRE-214 audit §8.7.

---

## Program Architecture (L0–L3) — `docs/specs/SESHAT_PROGRAM_ARCHITECTURE.md`

As of 2026-06-06 (FRE-504) the portfolio is organized as **substrate pillars vs feature consumers** across four layers. This is the forward-looking organizing layer over the legacy Wave A–J sequence (below, retained as historical record). Live Linear projects map to layers:

| Layer | Linear project(s) | Role |
|-------|-------------------|------|
| **L0 — Observability substrate** | **Telemetry Surface Audit** (NEW, Approved 2026-06-08) — ES mapping↔code↔dashboard reconciliation; FRE-533 (A1, Opus) → 534/535 → 536–539; local infra, session held. · **Observability Foundation** — 451 taxonomy ✅ · 452 ledger ✅ · 506 gate telemetry ✅ · 513 ADR-0088 spine ✅ · 514 REST read ✅ · 515 delegate used/discarded ✅ · 519 sub-agent read surface ✅ · 505 sub-agent auditability ✅ (verified+closed 06-08) · **453** eval set re-sequenced → blocked by **541** (driver) · **OPEN (Approved):** 518 live-render gap (High bug) · 517 per-topology rows · 523 eval-mode memory pipeline (bug) · 522 eval⇄PWA reconciliation · **Needs Approval:** 541 eval conversation driver + `clarification_requested` (Opus, blocks 453) | Makes *actual* traversal observable; gates reconciliation + shipping-to-default. Governed by **ADR-0088** (Accepted). |
| **L1 — Intended-traversal matrix** | *(folded into Observability Foundation)* — FRE-453 + knowledge-access column + decomposed build/teach case | Normative spec; authored *in parallel* with L0 (declaring intent needs no telemetry). |
| **L2 — Substrate pillars** | **Memory Recall Quality** (ADR-0087) · **Seshat Inference Architecture** (ADR-0082 — plumbing + planner reliability, incl. **FRE-502**) · **ADR-0081 Extended — Context & Memory Injection Quality** · **Artifact Execution Security** (ADR-0089 **Implemented 2026-06-07** — core 509✅510✅511✅512✅ live+verified; **Addendum A merged** PR #188 = curated `/lib/` toolkit → impl tickets FRE-526–532 Approved; FRE-524/498 canceled, FRE-497 re-homed) | Cross-cutting capabilities with many consumers. All three live pillars **Approved**. |
| **L3 — Consumers** | **Seshat Pedagogical Architecture** · **Turn Cost & Latency Optimization** · **Turn Reliability Hardening** | Features standing on the substrate. |

**Reconciliation loop (L0↔L1):** intended matrix vs actual ledger; every gap resolved loudly in one of two explicit directions ("loud or it rots"). Currently a *principle*, not yet a running control system (operationalizing it is itself a future L0/L1 deliverable).

**Active sequence — visibility-first (decomposition first-run fix queue):** Wave 0 (SEE) = **FRE-501 ✅** (live cost+status meter, PR #171, deployed) · FRE-505 · FRE-506 — *build/adr*. Wave 1 = FRE-502 (planner reliability). Wave 2 = FRE-503 (proactive depth for build/teach). Parallel = FRE-500 (sandbox flag bridge). adr = **FRE-504** ✅ **done** (spec PR #172; ADR-0088 Accepted · 0089 Implemented · 0090 Proposed — all landed; closed 2026-06-08, threads 2/3/7 routed to Memory Recall / Inference pillars).

**Reconciliation — RESOLVED by adr session (2026-06-06; do not re-resolve):**
1. **FRE-502** (planner reliability) — ✅ **MOVED** to **Seshat Inference Architecture** (spec §4 mechanism-robustness routing); was in Turn Cost.
2. **Artifact Execution Security** (L2 pillar, ADR-0089) — **deferred by design**: creating the project + re-homing FRE-497/498/499/500 (currently Turn Cost) happens at **ADR-0089 authoring** (adr session), since the ADR defines the pillar's shape — creating it now would approve a boundary before its ADR. No master action; tracked interim.
3. **FRE-453** — **resolved, no change**: filed in **Observability Foundation**, which owns the L1 matrix. L0 and L1 are **co-located** (the two halves of the reconciliation loop; there is no separate L1 project) — so it *is* "with L1." Optional future polish (owner's call): two milestones ("L0 — Observation" / "L1 — Intended matrix") to make the split visible.
4. **3 pillars approve** — ✅ **DONE**: Memory Recall Quality, Seshat Inference Architecture, ADR-0081 Extended all **Approved** (restructure pass, owner-authorized).

*Restructure provenance:* Observability Foundation created (Approved); lifted FRE-451/452/453 (from Pedagogical M2), FRE-505 (from Turn Cost), FRE-506 (no prior project) into it.

*Re-home pass (2026-06-10, owner-authorized — "easier to trace and sequence"):* the two near-complete incident projects (**Turn Cost & Latency** ~72% closed, **Turn Reliability Hardening** ~63% closed) stay as-is to wind down; only the open tickets with a clear dependency home moved out — **FRE-507** (event-driven cost streaming) → **Observability Foundation** (ADR-0088 D3 / ADR-0076 lineage; reassess vs what FRE-513 already shipped at the cost boundary); **FRE-495** (local sub_agent context_length), **FRE-472** (conversational capability-trap research), **FRE-492** (HITL dynamic allow-gate for discovery sub-agents) → **Seshat Inference Architecture**. Left in place as general turn-work: FRE-477/487 (Turn Cost ergonomics), FRE-497/474 (Turn Reliability). Not folded into the 3 active substrate pillars (different charter; would dilute scope + lose incident provenance).

---

## Active Design Threads

Four threads carved from the FRE-389 on-device review (2026-05-28). All **Approved**. FRE-398 (bubble-up errors) ✅ Done.

| Thread | Issue | Scope |
|--------|-------|-------|
| **Dynamic artifacts** | [FRE-397](https://linear.app/frenchforest/issue/FRE-397) | Diagrams now → interactive later (Tier 1 SVG → Tier 2 sandboxed JS → Tier 3 JSX). |
| **Adaptive limits & error recovery** | [FRE-399](https://linear.app/frenchforest/issue/FRE-399) | ~~524 root cause fixed (`cbd6f45`).~~ Layer 3 ✅ (ADR-0083, PR #139): cross-tunnel SLM health monitor, enriched `/api/inference/status`, executor error-reason hint. Children: FRE-444 (Mac-side enrichment), FRE-443 (L2 cloud fallback — gate: genuine failure observed), FRE-445 (dynamic thresholds, coordinate FRE-391). |
| **E2E testing (transport/UI/error)** | [FRE-400](https://linear.app/frenchforest/issue/FRE-400) ✅ Done | PR1 ✅ (PR #140): WS harness + 16 tests + CI. PR2 ✅ (PR #141): 59 Vitest component+hook tests. PR3 ✅ (PR #142): 4 Playwright e2e browser tests. FRE-390 closed (subsumed). |
| **Planner-executor split** | [FRE-401](https://linear.app/frenchforest/issue/FRE-401) | Reasoning model plans; subagents execute in isolated context. ADR required before implementation. |

**Recommended order**: FRE-434 ✅ → FRE-377 ✅ → FRE-408 ✅ → FRE-409 ✅ → FRE-399 L3 ✅ → FRE-400 ✅ (3/3 PRs) → **[FRE-384](https://linear.app/frenchforest/issue/FRE-384) / [FRE-383](https://linear.app/frenchforest/issue/FRE-383) (next — High bugs)** → FRE-432 → FRE-397 Tier 2.

**Standalone (Approved)**: FRE-394 (PWA SW dead code), FRE-395 (PWA ESLint).

---

## Upcoming — Wave Sequence

| Wave | Theme | Status | Key Issues | Notes |
|------|-------|--------|------------|-------|
| **A** ✅ | Dev loop & hygiene | Done | FRE-309 · FRE-185/189/320/321/312/308 | Shipped 2026-05-08 |
| **B** ✅ | Self-observation | Done | FRE-301 ✅ · FRE-300 ✅ · FRE-319 ✅ · FRE-269 ✅ · FRE-326 ✅ | |
| **C** ✅ | Security | Done | FRE-225 ✅ | |
| **D** | Architecture | Planning ✅, impl deferred | FRE-214 ✅ · FRE-238 · FRE-240 · FRE-241 · FRE-236 · FRE-338–340 | Deferred per audit §8.7 |
| **E** ✅ | Identity & write surface | Done | FRE-213 ✅ · FRE-227 ✅ · FRE-371 ✅ · FRE-368 ✅ · FRE-342 ✅ · FRE-343 ✅ · FRE-344 ✅ · [FRE-369](https://linear.app/frenchforest/issue/FRE-369) (Approved) | FRE-369 uploads next. |
| **F** | Self-improvement | Partial | [FRE-328](https://linear.app/frenchforest/issue/FRE-328) 🅿️ · FRE-385 ✅ · FRE-387 ✅ · FRE-226 · FRE-234 | Gate reset 2026-05-26 → review ≥ 2026-06-09. CL 2-week promotion gate ~2026-06-09. |
| **G** | Cleanups & gates | Partial | FRE-265 ✅ · FRE-299 ✅ · FRE-337 ✅ · [FRE-314](https://linear.app/frenchforest/issue/FRE-314) · FRE-311 | FRE-311 parked on FRE-302 |
| **H** | Memory / context value | Partial | [FRE-375](https://linear.app/frenchforest/issue/FRE-375) ✅ → [FRE-374](https://linear.app/frenchforest/issue/FRE-374) ✅ → [FRE-376](https://linear.app/frenchforest/issue/FRE-376) ✅ → [FRE-377](https://linear.app/frenchforest/issue/FRE-377) ✅ (soak ~06-03) → [FRE-381](https://linear.app/frenchforest/issue/FRE-381) (Needs Approval) → FRE-178 → FRE-179 → FRE-180 · FRE-230 | FRE-377 shipped (PR #135); AC-5 1-day soak pending. |
| **I** ✅ | Prompt observability | EPIC Done | [FRE-403](https://linear.app/frenchforest/issue/FRE-403) EPIC ✅ · FRE-404–409 ✅ (P0–P5) · P6 (DSPy opt, optional) · FRE-183 · FRE-184 | **EPIC complete 2026-06-02** (P0–P5 shipped+verified). P6 optional/future-gate (≥200 rated eval turns). FRE-183/184 separate Wave I items. |
| **J** ✅ | Eval methodology hardening | Done | FRE-329–335 all shipped | |

---

## Pending Verification

- **FRE-557** ⏳ OPEN — deployed 2026-06-15 (PR #217, `e548545`); template/`dynamic:false`/types verified, joinability clean. **Remaining (read-only, next natural turn):** a doc in `agent-monitors-projector-health-*` with `observation_complete:true` + `model_calls_received == COUNT(api_costs WHERE trace_id)`. Master won't fire a turn. On confirm → close Done.
- **FRE-523** ⏳ OPEN — deployed (PR #208). Capture-layer AC ✅ (run `fre523-verify-01`). **KG-half unblocked by FRE-560 (2026-06-14):** the morning eval backlog drained on the first consolidation post-deploy — **AC-1 (eval→KG) ✅** (trace `4612bff6` is a Turn node w/ entities), **AC-4 (provenance) ✅** (`Turn.properties.eval_mode=true` eval / `false` real), **AC-2 ✅**. **Only AC-3 (cross-run recall) remains → needs an owner-run pass-2** (a second eval run recalling pass-1's now-loaded content; requires firing live turns — master will not initiate). On AC-3 green → close 523 Done. Remaining procedure:
  1. Run an eval pass with `channel=EVAL` against the deployed gateway (e.g. the `fre453-baseline` set). Use the owner's designated test email — never the injected CC userEmail.
  2. **AC-1:** confirm `agent-captains-captures-*` **and** `agent-captains-captures-subagents-*` contain docs for the run's traces, and the consolidator wrote Turn/Entity nodes to Neo4j for those eval traces (the bug was 0/18 extraction on `fre453-baseline-02`).
  3. **AC-4:** confirm those capture docs carry `eval_mode:true` and the KG `TurnNode.properties.eval_mode` is `true`.
  4. **AC-3:** run a **second** eval pass; confirm it recalls content from the first run's sessions (cross-run recall probe — feeds ADR-0087 / FRE-435).
  5. **AC-2** already covered (unchanged `tools/linear.py` gate + regression test). Sanity: confirm **no** Linear issues were filed off eval prompts (promotion skip).
  - On all green → close **FRE-523 Done** with the evidence snippet; bump this section. If captures don't appear or KG stays empty → file a follow-up, do **not** mark Done.
- **FRE-468** ✅ DONE — post-deploy verified 2026-06-04: no Anthropic 400, `cache_read_tokens=17,772` on round 2, `cache_control_cap_enforced` never fired. Fix confirmed live.
- **FRE-473** ✅ DONE — post-deploy verified 2026-06-04: `cache_read_tokens=17,772` unchanged vs FRE-468 baseline; no §D2 regression; persisted history now provider-neutral.
- **FRE-408** ✅ DONE (owner accepted real-telemetry equivalent — 3 buckets on real ES traces). Optional Mac harness smoke remains belt-and-suspenders, not blocking.

---

## Turn Reliability Hardening (2026-06-04 incident) — winding down (build-to-close)

All five from the `cache_control 5>4` post-mortem (PR #150). FRE-468 is Urgent and first. **2026-06-10:** after the re-home pass (FRE-472 → Inference), the two residuals **FRE-497** (self-correcting gates, ADR) + **FRE-474** (cross-provider cache research) were **Approved** to build-to-close; project closes when both ship. Turn Cost & Latency likewise winds down via **FRE-477** + **FRE-487** (both Approved).

| Ticket | Priority | Tier | What |
|--------|----------|------|------|
| [FRE-468](https://linear.app/frenchforest/issue/FRE-468) ✅ | **Urgent** | Sonnet | **DONE** (PRs #151+#152, `6fb0d2c`) — `_strip_cache_control` + `_enforce_cache_control_cap`; 11 tests; post-mortem amended. **Deploy + verify pending.** |
| [FRE-469](https://linear.app/frenchforest/issue/FRE-469) ✅ | **High** | Sonnet | **DONE** (PR #154, `424c27b`) — `_TOOL_INTENT_PATTERNS` artifact/build extension; verified live: `task_type=tool_use, signals=['tool_intent_pattern']`. |
| [FRE-470](https://linear.app/frenchforest/issue/FRE-470) ✅ | Low | Sonnet | **DONE** (PR #156, `696e5e6`) — exit 141 treated as success only on a top-level pipe (`_has_top_level_pipe`); standalone 141 still fails; `note` field added. 7 unit + 5 real-bash integration tests. Deployed + verified live (code in container, health green). |
| [FRE-471](https://linear.app/frenchforest/issue/FRE-471) ✅ | Low | Sonnet | **DONE** (PR #157, `a259503`) — `_truncate_plan` boundary-aware trim + anti-fabrication notice (never raises on oversize); cap 8000→16000; `plan_truncated`/`plan_original_length` flags; empty plan still raises. Deployed + verified live (`_MAX_PLAN_CHARS=16000` in container, health green). |
| [FRE-472](https://linear.app/frenchforest/issue/FRE-472) | Research | Opus | `conversational` capability trap: tool-runway floor, validation-retry budget, thinking/budget interaction |

---

## Immediately Actionable (approved, no gate)

**Three-project parallel build — Telemetry Surface Audit ‖ Observability Foundation ‖ Artifact Execution Security.** Three largely-independent surfaces → 3 lanes. **Live status (EOD 2026-06-08):**

```
Lane T (Telemetry · local, NO prod deploy)
  533✅ ⟶ ┬ 534✅ ┐       A1/A2/B1 done + applied live; 536–539 BUILDABLE
  (done)  └ 535✅ ┴⟶ 536 · 537 · 538 · 539
  536✅ 537✅ 538✅ 539✅ — **ALL C-dashboards live; Telemetry build phase (533–539) COMPLETE**
  follow-ups: 540 (A3 CI checker) ✅ done → 555 (gate-flip) Approved · all Approved (build-ready): 543 (ILM) · 544 (dyn-field bound) · 545 (routing_decision) · 546 (cost-cache import) · 547 (cap-util) · 548 (topology) · 550 (joinability breakdown) · 552 (session_id on errors)

Lane A (Artifact toolkit) — 526–531 ✅ COMPLETE; **FRE-525 umbrella Done**
  526✅ → 527✅ → 528✅ → 529✅ → 530✅ → 531✅ (E2E, closed 525)
  532✅ PWA rendering convergence (deployed — hljs/KaTeX/mermaid pinned, CACHE_NAME v21)
  Remaining (separate follow-ups): 549 PWA export trigger · 551 extend E2E (three.js+fonts) ·
  cross-repo CF-token auth for live inline /lib/ export
  Toolkit live: shelf hosted+advertised+metered+exportable, E2E-verified under CSP

Lane O (Observability)
  518✅ ⟶ 523 ⟶ 517 ⟶ 522     505✅ master verify+closed
 (deployed) (next)            453 → re-sequenced behind 541 (Needs Approval)
```

### Two-worktree dispatch (2026-06-13 refresh) — file-domain split, no A/B collision

*Model per ticket (Tier→model, MODEL_ROUTING_POLICY): **[O]** Opus · **[S]** Sonnet · **[H]** Haiku. Escalate Sonnet→Opus on 3 failed attempts / API-shift.*

**Lane A — Telemetry surface** (ES templates · Kibana · cost_gate · tools governance; local-mostly):
1. FRE-544 ✅ → 2. FRE-559 ✅ → 3. FRE-546 ✅ → 4. FRE-550 ✅ → 5. FRE-556 ✅ → 6. FRE-558 ✅ (deployed, PR #233) → **7. FRE-567 [S]** generic numeric dynamic_template ← next.

**Lane B — Observability/topology/eval/ledger + Artifact** (projector · route-trace ledger · eval harness · artifact_tools · PWA):
1. FRE-545 ✅ → 2. FRE-557 ✅ → 3. FRE-507 ✅ → 4. FRE-568 ✅ → 5. FRE-570 ✅ → 6. FRE-576 ✅ (deployed, PR #232) → **FRE-577 [S]** occupancy eval ← next → **572 [S]** severity; also queued {**571 [H]** ES maps · **573 [S]** PWA two-lane}. Also queued: **FRE-522 [S]** eval⇄PWA · **FRE-542 [S]** PWA dedup · **FRE-551 [S]** artifact E2E · **FRE-566 [S]** zero-delivery monitor · **ADR-0091 eval chain:** **561 [H]** ∥ **562 [S]** → **563 [S]** → **564 [S]** → **FRE-453 [S]**.

**adr session (worktree-adrs) — observability spec-first (owner: "finish infrastructure + observability first"):**
- ✅ **FRE-541** — **ADR-0091 shipped** (PR #216, Proposed; amends ADR-0084 §D4). Umbrella stays In Progress; impl now in 561–564. → **FRE-561/562/563/564 Approved 2026-06-15** — the eval-validity build chain → **Lane B** (serial: **561** doc-mirror T3 ∥ **562** dataset → **563** driver+detector → **564** report+validation; **564 unblocks FRE-453**).
- ✅ **FRE-554** — **ADR-0092 shipped** (PR #219, Proposed; owner-interviewed). Done. → impl chain **Approved 2026-06-16 → Lane B**: **568** (projector session agg) → **570** (A/B/D markers + 4 fields) → {**571** ES maps · **572** backend monitors · **573** PWA two-lane}. **FRE-569** (mechanism-C carve-out, T1) **HELD** (Needs Approval — owner deferred; C parked). adr session now free.

**Context-compression cross-audit (NEW 2026-06-19, Approved) — project: ADR-0081 Extended.** Surfaced by the owner's external LLM-training-course authoring (`~/github/llm-course`) — a line-by-line read of the shipped compression pipeline against ADR prose. Both `agent-filed`.
- **FRE-576** (T1) — 5 findings: **F2 🐞** within-session recap `SUMMARY_ROLE="system"` is silently dropped by the role-fixer (compression discards its own output) → unify on `assistant` like `build_frozen_reset`; **F3 ⚙️** cost-optimal reset scheduler quality term inert (`quality_slope=0.0` hardwired) → wire/observe (relates to 570/572 quality signal); **F4 📝** dead-by-default `compressed_summary` re-insertion branch (frozen layout) → delete or flag; **F5 📝** "≤200 words" vs `max_tokens=512` reconcile; **F1 → FRE-577**. Touches `within_session_compression.py`/`cache_reset_scheduler.py`/`context_window.py`/`context_compressor.py`. ⚠️ **collides with FRE-570 on `within_session_compression.py`** — serialize (recommend **576 first**: fix the role bug before 570 adds B/D markers).
- **FRE-577** (T2) — long-session **occupancy-curve eval** (EVAL-04's 2.5% is a measurement gap — only 1,625 tokens, never filled the window). **Blocked by FRE-570** (reads the new compaction marker events); **feeds FRE-572** (gateway severity model reasons on un-exercised evidence today). Dep chain: **570 → 577 → 572**.
- Sequencing (**owner 2026-06-19: interleave after 570**): **570 (markers) → 576 (audit fixes) → 577 (occupancy) → 572 (severity consumes the occupancy data)**. 576 serializes after 570 on `within_session_compression.py` (orthogonal logic — 570 = marker emission, 576 = recap role-fix). 576 is Opus-tier (per-finding judgment). Lane B orchestrator/compression sub-stream, not Lane A.

**In flight / parked:**
- ✅ **FRE-560** — Done (PR #214, deployed+verified). KG write pipeline healthy.
- **FRE-523** — In Progress; KG-half AC-1/AC-4 met (560 drained the backlog); only AC-3 (owner-run pass-2) remains (§ Pending Verification).
- **Memory Recall program (FRE-488/489/490/491/493/494)** — **PARKED until the infra+observability streams finish** (owner decision 2026-06-14). Now technically unblocked (560 populates the KG; 488 Approved + meaningful; 493/494 are ADR/research) — re-assess 489–491 when the streams complete.
- Turn Cost/Reliability closes (477/487/497/474) — out of scope; Turn projects wind down separately.

**Collision rules:** topology projector (545/557/507) stays in Lane B, serial — different files from FRE-560 (scheduler/executor/app), so they may run concurrently. PWA is shared (522/542/554) — one lane owns it at a time, bump `CACHE_NAME` on shell deploys. `pytest` lock = one `make test`. Merge server-side; deploy one-at-a-time from main.

**Capstone (LAST, either worktree once free):** **FRE-555** flip reconciliation checker → hard CI gate — **gated on ALL emit-gaps merged** (544/545/546/550/558/559). Closes Telemetry Surface Audit + realizes ADR-0090 D5.

**Stream C — Dependency-security remediation (SOLO, NOW — owner re-prioritized 2026-06-19: "Pausing A and B — Stream C takes priority"):**
- **FRE-578 [S] ✅ Done + deployed** (PRs #224 Python + #225 PWA, `1063820`) — 1 critical (litellm auth-bypass) + 7 high (pyjwt, starlette ×2, cryptography, python-multipart, undici, vite) all cleared; **Dependabot crit+high → 0**; starlette major bump booted clean; uv `override-dependencies` for transitive floors.
- **FRE-579 [S] ← next (Approved):** second pass — 24 moderate + 12 low (now ~17 mod + 7 low after 578's transitive clears). Python (aiohttp≥3.14.1 / idna≥3.15 / pip≥26.1; **diskcache no upstream fix → document**) PR + PWA (dompurify/katex/brace-expansion/js-yaml; **katex `--force` needs owner OK**) PR. Solo, branches off `1063820`. Acceptance: open alerts ≤12 (lows) or documented rationale.
- Runs **solo** (one worktree — shared lockfiles, isolated verification). **Lanes A/B PAUSED** (FRE-550 / FRE-570 held mid-queue) until Stream C drains (579).

**Session assignment now (2026-06-20):** Stream C complete; **FRE-550 ✅ + FRE-570 ✅ deployed** (In Progress pending live data). **build → Lane A → FRE-567 [Sonnet]** (generic numeric dynamic_template; 558 ✅) · **build2 → Lane B → FRE-577 [Sonnet]** (long-session occupancy eval — F1, reads 570's compaction markers; feeds 572) · adr free. Lanes A/B file-disjoint → parallel. Memory Recall still parked.

**Collision rule:** anything touching the topology projector/ledger (517, 548) stays in B and serial. PWA is shared (522/532/PWA-side of 551) — one lane at a time. `pytest` lock = one `make test` at a time. Master merges server-side + deploys one-at-a-time from main.

- **Lane T — Telemetry** (local ES/Kibana; **no prod deploy**): FRE-533 ✅ (1023-row inventory) → FRE-534 ✅ (templates corrected + applied+verified live, PR #194) ‖ FRE-535 (dashboard triage) **← buildable** → FRE-536/537/538/539 (cost · ledger+topology · joinability+SLM-health · turn/E2E/envelope) **← now unblocked**.
- **Lane A — Artifact toolkit** (ADR-0089 Add. A merged, PR #188): FRE-526 ✅ → FRE-527 ✅ (`/lib` hosted + `verify-lib` green) → {FRE-528 prompt ‖ FRE-529 skill ‖ FRE-530 export} **← buildable now** → FRE-531 (E2E); FRE-532 (PWA) independent. FRE-525 umbrella closes with FRE-531.
- **Lane O — Observability**: FRE-518 ✅ (live-render bug, deployed) → FRE-523 (eval-mode memory bug) **← next** → FRE-517 (per-topology rows) → FRE-522 (eval⇄PWA + tool-render). Non-build: FRE-505 ✅ verified+closed · FRE-453 re-sequenced behind FRE-541 (rubric waits on the conversation driver).

**Deploy cadence (master, owner-approved, one-at-a-time from main):** *Gateway* (526/528/523/517 + 518 backend) — batch by surface; joinability probe after any emit/schema/memory ticket. *Worker/terraform* (527/530) — independent surface. *PWA* (518/522/532) — serialize, bump `CACHE_NAME`, gateway rebuild ≠ PWA deploy. *Telemetry* — no prod deploy; local apply + commit templates + NDJSON.
**Contention guardrails:** PWA is the shared resource (518/522/532) → one lane owns it at a time, land 518 first. `pytest` lock = one `make test` at a time → throughput cap. `artifact_tools.py` touched by 526+528 (same lane, sequential). Merge server-side; deploy from main one at a time.

| Ticket | Proj | Pri | Tier | What |
|--------|------|-----|------|------|
| [FRE-518](https://linear.app/frenchforest/issue/FRE-518) ✅ | Obs | **High** bug | Opus | **DONE** — per-session emit lock restores enqueue-order==seq-order (PR #192); deployed 2026-06-08 (gateway live: `emit_done`+`_get_emit_lock`, health green). Root cause: FRE-513 projector = 2nd concurrent emitter on a latent ADR-0075 seq-dedup edge. |
| [FRE-523](https://linear.app/frenchforest/issue/FRE-523) | Obs | bug | Sonnet | **DEPLOYED 2026-06-12** (PR #208, `46a68c1`) — `eval_mode` redesign: capture/event/reflection/extraction→KG RUN during eval (primary+sub-agent), provenance stamped, promotion skips eval entries (Linear leak closed), `tools/linear.py` gate unchanged; ES `eval_mode:boolean` pinned live. **In Progress** pending eval-run verification of AC-1/3/4 (capture+KG write + cross-run recall — owner-driven). Unblocks recall testing (ADR-0087). |
| [FRE-517](https://linear.app/frenchforest/issue/FRE-517) | Obs | Med | Sonnet | ADR-0088 seam: per-topology `(trace_id, task_id)` rows — one per sub-agent/segment; generalize read surface to multi-row; per-segment cost. |
| [FRE-522](https://linear.app/frenchforest/issue/FRE-522) | Obs | — | Sonnet | Reconcile eval-run ⇄ PWA: report-case→session deep links + **fix tool-use render gap** (ledger 15/18, PWA 0). |
| [FRE-505](https://linear.app/frenchforest/issue/FRE-505) ✅ | Obs | High | Sonnet | **DONE** — verified live: 20 records in `agent-captains-captures-subagents-2026-06-07` with `memory_in_context` + `full_output` + `truncation_ratio` (PR #179/#180). |
| [FRE-541](https://linear.app/frenchforest/issue/FRE-541) | Obs | — | Opus | **Approved (2026-06-10)** — eval conversation driver + `clarification_requested` result type: carry each case to a natural end; separate completion-status from outcome-quality. Unblocks 453. (Finding: baselines' `not_fired_within_window` conflates quality-miss with model-paused-for-input.) |
| [FRE-453](https://linear.app/frenchforest/issue/FRE-453) | Obs | Med | Sonnet | **Re-sequenced to backlog** — harness done (PR #183) but single-shot baselines conflate quality with harness-completion; rubric pass waits on **FRE-541** (driver). Not an owner-rubric-ready item anymore. |
| [FRE-526](https://linear.app/frenchforest/issue/FRE-526) ✅ | Art | — | Sonnet | **DONE** — meter fix (PR #190); deployed 2026-06-08 (gateway rebuilt, code live, joinability green, agent-logs template carries the 3 `long` fields). FRE-498 Canceled (superseded). |
| [FRE-527](https://linear.app/frenchforest/issue/FRE-527) ✅ | Art | — | Sonnet | **DONE** — `/lib/` hosted on the Worker (terraform); `make verify-lib` green from VPS (9/9 reachable + correct MIME + nosniff; paged.js eval-gated→FRE-531). Verifier PR #191. **Unblocks 528/530/531.** Master follow-up: fold `verify-lib` into the deploy gate. |
| [FRE-528](https://linear.app/frenchforest/issue/FRE-528) ✅ | Art | — | Sonnet | **DONE** — `_HTML_GENERATION_SYSTEM_PROMPT` reframed to advertise the curated `/lib/` shelf + native typography (PR #196); **deployed+verified live** (prompt in container, health green). Sealed-box constraints preserved (no arbitrary CDN/network/storage). |
| [FRE-529](https://linear.app/frenchforest/issue/FRE-529) ✅ | Art | — | Sonnet | **DONE** — `docs/skills/artifact-design.md` runtime-guidance source-of-truth (PR #198, docs-only); manifest-driven drift-guard test lockstep w/ 528. |
| [FRE-530](https://linear.app/frenchforest/issue/FRE-530) ✅ | Art | — | Sonnet | **DONE** — export-to-standalone `/export` endpoint (inline SRI + substitute CDN+SRI; SSRF-guarded; PR #199); **deployed+verified** (route 401, joinability green). Inline `/lib/` fetch needs CF token auth (laptop, cross-repo); substitute works now. |
| [FRE-531](https://linear.app/frenchforest/issue/FRE-531) ✅ | Art | — | Sonnet | **DONE** — E2E render harness (Chromium+WebKit) under exact CSP + offline export + paged.js eval-free (PR #202); live `verify-lib` 9/9. **Closed FRE-525 umbrella** (toolkit complete). |
| [FRE-532](https://linear.app/frenchforest/issue/FRE-532) ✅ | Art | — | Sonnet | **DONE** — PWA toolkit convergence (hljs/KaTeX/mermaid pinned; trust-gradient preserved); **deployed** (seshat-pwa, CACHE_NAME v21). katex@0.16.11 advisory mitigated (trust:false). |
| [FRE-533](https://linear.app/frenchforest/issue/FRE-533) ✅ | Tel | — | Opus | **DONE** — reconciliation inventory (PR #193): 1023 (field,family) rows; 643 emitted-but-unmapped, 30 traps, **14 broken panels / 6 of 12 dashboards** (`.keyword` aggs on bare-keyword → silent empty). `docs/research/` + CSV + reusable audit script. **Unblocks 534/535/537/538/540.** |
| [FRE-534](https://linear.app/frenchforest/issue/FRE-534) ✅ | Tel | — | Sonnet | **DONE** — templates corrected + **applied+verified live** (PR #194): `ms_fields_as_float`, captains 3-way split (subagents@120), insights/slm-health templates w/ keyword join keys; `denial_reason` kept keyword for the donut. New-indices-only, no backfill. Unblocks 536–539. |
| [FRE-535](https://linear.app/frenchforest/issue/FRE-535) ✅ | Tel | — | Sonnet | **DONE** — triage 12 dashboards fixed/retired + **imported+verified live** (PR #195, harness PASS 0 silent-empty). Filter-aware harness caught 20 broken vs A1's 14; hardened `import_dashboards.sh`. Spawned **545** (routing_decision emit) + **546** (prompt-cost-cache import fmt) — both Needs Approval. |
| [FRE-536](https://linear.app/frenchforest/issue/FRE-536) ✅ | Tel | — | Sonnet | **DONE** — C1 cost & budget dashboard + cost_gate `*_usd` `double` emit fix (PR #197); **deployed+applied+verified** (gateway emit live, template `double`, dashboard live, joinability green). Cap-util deferred → 547. |
| [FRE-537](https://linear.app/frenchforest/issue/FRE-537) ✅ | Tel | — | Sonnet | **DONE** — C2 traversal-ledger & gate-decision dashboard (PR #200); **imported+verified live** (6 panels, A1-trap-guarded). Topology deferred → 548. |
| [FRE-538](https://linear.app/frenchforest/issue/FRE-538) ✅ | Tel | — | Sonnet | **DONE** — C3 monitors dashboard (joinability + SLM-health) imported+live (PR #201); handled cross-time SLM mapping straddle. Per-substrate breakdown deferred → 550. |
| [FRE-539](https://linear.app/frenchforest/issue/FRE-539) ✅ | Tel | — | Sonnet | **DONE** — C4 turn/session/artifact-envelope dashboard imported+live (PR #204). **Last C-ticket — Telemetry build COMPLETE.** Honest data-limits documented (readiness banner). |


| [FRE-384](https://linear.app/frenchforest/issue/FRE-384) ✅ | **High** | Sonnet | **DONE** (PR #143) — `CAST(:tag_filter AS text[]) IS NULL` fixes AmbiguousParameterError. `init.sql` FK order fixed. Verified live. |
| [FRE-383](https://linear.app/frenchforest/issue/FRE-383) ✅ | **High** | Sonnet | **DONE** (PR #144) — anti-fabrication rule in `_TOOL_RULES`; 4 regression tests; baseline 1857→2176. |
| [FRE-377](https://linear.app/frenchforest/issue/FRE-377) ✅ | Medium | Sonnet | **DONE** (PR #135) — canonicalize TaskCapture token fields. AC-5 soak passed 2026-06-03. |
| [FRE-369](https://linear.app/frenchforest/issue/FRE-369) | Medium | Sonnet | User-upload UX in PWA with presigned PUT to R2 (images/files in chat). Spec: ADR-0069 + ADR-0070. |
| [FRE-314](https://linear.app/frenchforest/issue/FRE-314) | Medium | Sonnet | `feedback_history/` retention policy in DataLifecycleManager. |
| [FRE-350](https://linear.app/frenchforest/issue/FRE-350) | Medium | Opus | Post-deploy reflection-surfacing eval. Gate opened 2026-05-24 — startable now. |
| [FRE-349](https://linear.app/frenchforest/issue/FRE-349) | Medium | Opus | Surface actionable Insights in agent context (G3 from FRE-346). |
| [FRE-391](https://linear.app/frenchforest/issue/FRE-391) | Medium | Opus | Dynamic `max_tokens` based on tool/task context — addresses artifact truncation root cause. |

**Protect-live-rollout cluster (approved 2026-06-02) — ✅ COMPLETE (3/3 shipped + deployed + verified):**

| Ticket | Priority | Tier | What |
|--------|----------|------|------|
| [FRE-440](https://linear.app/frenchforest/issue/FRE-440) ✅ | — | Sonnet | **DONE** (PR #132, merge `e428e6e`) — pinned frozen-layout default in tracked config; full suite 2900✅; gateway reads `True`. |
| [FRE-437](https://linear.app/frenchforest/issue/FRE-437) ✅ | — | Sonnet | **DONE** (PR #133, merge `f8de7c3`, migrated `0008`) — persists per-tier cache token counts in `api_costs`. Audit reframe: `cost_usd` was never wrong (litellm already cache-aware); gap was discarded tier counts. Verified on real cloud turn (cache_creation 8,665). |
| [FRE-436](https://linear.app/frenchforest/issue/FRE-436) ✅ | — | Sonnet | **DONE** (PR #134, merge `f4ff9ee`) — `/chat` now persists + honours `execution_profile` (server-authoritative, ADR-0079). Verified: new session persists `cloud`; follow-up `profile=local` ignored, stays cloud. |

**Calendar-gated (approved but not yet startable):**
- **FRE-328** — naming-stability data review. Gate ≥ 2026-06-09 (2 weeks clean production data with agent-noun taxonomy).
- **FRE-381** (Needs Approval) — Stage 2 consolidator decoupling. Requires ADR-0074 §I5 amendment + post-FRE-380 soak data.

---

## Needs Approval

| Ticket | Priority | Tier | What |
|--------|----------|------|------|
| [FRE-523](https://linear.app/frenchforest/issue/FRE-523) | Bug | Sonnet | **Redesign `eval_mode` suppression (owner decision 2026-06-07): memory pipeline (capture/reflection/extraction) must RUN during eval** — suppressing it made pedagogical-continuity/recall untestable ("error in test planning"); only external side effects stay suppressed; fix primary/sub-agent inconsistency; EVAL provenance on derived content. Project: *Observability Foundation*. |
| [FRE-521](https://linear.app/frenchforest/issue/FRE-521) | — | Haiku | **PWA: per-session turn count** in the session UI + visually flag `channel=EVAL` sessions. Owner note from the FRE-453 baseline review (2026-06-07). Project: *VPS/Cloud Architecture Stabilization*. |
| [FRE-522](https://linear.app/frenchforest/issue/FRE-522) | — | Sonnet | **Eval ⇄ PWA reconciliation**: deep links report-case→session, run/case context on EVAL sessions, **+ fix the confirmed tool-use rendering gap** (ledger shows tools on 15/18 baseline cases; PWA shows none). Owner note from the FRE-453 baseline review. Project: *Observability Foundation*. |
| [FRE-435](https://linear.app/frenchforest/issue/FRE-435) | — | Opus | **Memory-recall research initiative** — quantify KG write + retrieval quality with a deep A/B harness (FRE-433 method); explore markdown/LLM-wiki retrieval. Owner's next big research item. |
| [FRE-438](https://linear.app/frenchforest/issue/FRE-438) | — | Sonnet | **PWA notes**: access + rendered-markdown (raw toggle) + promote/delete for notes & artifacts. |
| [FRE-439](https://linear.app/frenchforest/issue/FRE-439) | — | Sonnet | **Rating UX**: 0 → red pill + distinct "technical error" rating (un-conflate from quality-0). |
| [FRE-441](https://linear.app/frenchforest/issue/FRE-441) | — | Sonnet | **Eval tooling**: side-by-side pre/post (A/B) response comparison for human quality rating. Prereq for FRE-435. |
| [FRE-442](https://linear.app/frenchforest/issue/FRE-442) | — | Sonnet | **Behavior**: agent responses always include references/citations + make references a quality signal. |
| [FRE-464](https://linear.app/frenchforest/issue/FRE-464) | — | Opus | **ADR-0081 D4-trim**: skill-index format/size minimization (Pareto routing-accuracy vs tokens; DSPy candidate). Cost-trim on the now-cached index; does *not* gate cache-GREEN. Project: *ADR-0081 Extended*. |
| [FRE-465](https://linear.app/frenchforest/issue/FRE-465) | — | Opus | **ADR-0081 D5**: tiered virtual context — cold-tier on-demand `recall_session_history` (reinject context compression dropped). Open retrieval design Qs; likely needs ADR addendum. Project: *ADR-0081 Extended*. |
| [FRE-466](https://linear.app/frenchforest/issue/FRE-466) | — | Sonnet | **ADR-0081 D6**: optional message pin — never-compress + attention-aware placement; must respect the FRE-434 byte-identity invariant. Project: *ADR-0081 Extended*. |
| [FRE-381](https://linear.app/frenchforest/issue/FRE-381) | Medium | Sonnet | **Stage 2** consolidator decoupling — invert Turn-creation vs entity-extraction dependency; add `extractor_model` to `TurnNode`. Blocked-by FRE-380 ✅ + post-soak data. |
| [FRE-390](https://linear.app/frenchforest/issue/FRE-390) ✅ | Low | Sonnet | ~~Eval harness skips transport layer~~ — **Done** (closed as subsumed by FRE-400 PR1, 2026-06-03). |
| [FRE-467](https://linear.app/frenchforest/issue/FRE-467) | — | Opus | **Spatio-temporal memory** — location as episode dimension (from FRE-230; ADR required). |
| [FRE-432](https://linear.app/frenchforest/issue/FRE-432) | — | Opus | **Tier-aware model routing** — reconceived under ADR-0084 pedagogical north star; scope revision tracked FRE-450. |
| [FRE-468](https://linear.app/frenchforest/issue/FRE-468) | **Urgent** | Sonnet | `cache_control` ≤4 clamp (see Turn Reliability Hardening section above). |
| [FRE-469](https://linear.app/frenchforest/issue/FRE-469) | **High** | Sonnet | Classifier: artifact intent routing (see Turn Reliability Hardening section above). |
| [FRE-470](https://linear.app/frenchforest/issue/FRE-470) | Low | Sonnet | SIGPIPE false-fail (see Turn Reliability Hardening section above). |
| [FRE-471](https://linear.app/frenchforest/issue/FRE-471) | Low | Sonnet | `artifact_draft` truncate-with-warning (see Turn Reliability Hardening section above). |
| [FRE-472](https://linear.app/frenchforest/issue/FRE-472) | Research | Opus | Conversational capability trap research (see Turn Reliability Hardening section above). |
| [FRE-473](https://linear.app/frenchforest/issue/FRE-473) ✅ | **High** | Sonnet | **DONE** (PR #153, `72910ea`) — `_decorated_anthropic_copy()` deep-copies before decoration; 14 tests + LiteLLM contract test; `cache_read=17,772` unchanged post-deploy. |

---

## Key Dependencies

```
FRE-375 ✅ → FRE-374 ✅ → FRE-376 ✅ → FRE-377 (Approved, unblocked)
FRE-380 ✅ → FRE-381 (Stage 2, Needs Approval; post-soak data available)
FRE-178 → FRE-179 → FRE-180  (recall L2/L3/gap chain)
FRE-214 ✅ → FRE-238/240/241/236 + FRE-338–340 (unblocked, deferred §8.7)
FRE-302 ✅ → FRE-311 (budget auto-tuning, parked pending data)
FRE-346 ✅ → FRE-347 ✅ → FRE-348 ✅ → FRE-349 (G3, unblocked)
FRE-328 capture ✅ → naming-stability gate ≥ 2026-06-09 (agent-noun taxonomy deployed)
FRE-348 ✅ → FRE-350 (reflection eval, gate opened 2026-05-24, Approved)
FRE-403 EPIC ✅: FRE-404 ✅ → FRE-405 ✅ → FRE-406 ✅ → FRE-407 ✅ → FRE-408 ✅ → FRE-409 ✅ (all Done 2026-06-02)
ADR-0081 core chain ✅: D1 (FRE-422) → D4 split (FRE-431) → D2/D3 (FRE-434, live) — followups: FRE-464 (D4-trim) · FRE-465 (D5 cold-tier) · FRE-466 (D6 pin), all Needs Approval (project: ADR-0081 Extended)
FRE-227 ✅ → FRE-226 (self-updating skills)
FRE-391 (dynamic max_tokens) — independent; addresses artifact-truncation root cause
```

---

## Recently Completed

| Item | Date | Summary |
|------|------|---------|
| **ADR-0081 D1: volatility-gradient layout ✅ (D4-gated)** | 2026-06-01 | FRE-422, PR #120, deployed. System-prompt reorder STATIC→SEMI-STATIC→VOLATILE; layout-invariant test pins `tool_prompt` before `memory_section`. **Post-deploy eval: `orchestrator.primary` cache gate still RED — blocked on D4 (skill-index split).** Ticket reopened In Progress; cache-GREEN gate transfers to a forthcoming ADR-0081 D4 ticket. |
| **FRE-426: status + cost hydration ✅** | 2026-05-31 | PRs #113–#119, deployed (SW→v19). `GET /sessions/{id}` returns `context_tokens`/`context_max`/`cost_usd`; messages endpoint joins `user-turn-ratings-*` by `trace_id` (assistant-only); PWA `seedTurnStatus` hydrates meters + ratings on mount/switch. Verified context≈3193, cost $0.335. |
| **FRE-407 P3: per-turn 0–3 rating ✅** | 2026-05-31 | PR #112 + #113. Human-eval instrument joined to PromptIdentity on `trace_id`; default=2, un-rated imputed as 2; `user-turn-ratings-*` template + 90d ILM; PWA 4-segment `TurnRating`. The quality baseline enchained ahead of FRE-422/ADR-0081 D1. |
| **FRE-406 P2: cost/cache attribution + erosion alarm ✅** | 2026-05-31 | ES template `prompt_*` explicit mapping; `make cache-erosion-status` (Jaccard ≥ 0.9); Kibana saved objects. Live result: `orchestrator.primary` jaccard=0.200 [ERODED] — confirmed cross-turn KV reuse ≈ 0, justifying ADR-0081. |
| **FRE-374 purge→replay ✅** | 2026-05-30 | 14,213 entities + 11,984 relationships, 0 errors. Probe 1/2/5/6 green. Empty-desc 24.6% (was 42%); redundant edges 4.4% (was 9.3%). CostGate init + LiteLLMClient pool leak fixed en route. |
| **FRE-376 joinability gate ✅** | 2026-05-30 | ADR-0074 → Accepted. Retroactive 6/6 audit green; three probe-tool bugs fixed (legacy SSE exclusion, ws_ticket logger, three_way_mismatch escalation). |
| **FRE-412 entity dedup ✅** | 2026-05-30 | Threshold 0.85→0.92 + ALL_CAPS name-pattern guard. Prevents concept over-merging (e.g. `LLM_CALL`/`TOOL_EXECUTION` collapsing). |
| **FRE-405 P1: Prompt Identity ✅** | 2026-05-29 | PR #109. Every `model_call_completed` carries prompt callsite + component IDs + static/dynamic hash. Cache telemetry fixed (PR #110); `slm-requests-*` keyword index (PR #111). FRE-411 join 8/8 validated. |
| **FRE-421/417/415/414: cloud-path bugs ✅** | 2026-05-29 | PRs #105-108. Context meter uses active model's window; error card path-aware; `/no_think` Qwen-only (ADR-0080); input always writable; availability banner on down path. |
| **FRE-416/419: session profile desync ✅** | 2026-05-29 | PRs #102-104. Server-authoritative profile (ADR-0079); PATCH toggle; mount hydration; new-session hotfix (1056 rows backfilled). |
| **FRE-393/389: identity gate + constraint governance ✅** | 2026-05-28 | PRs #86-91. Scope-aware deny-by-default AST checker (70+→8 allowlist); constraint pause + DecisionCard + TurnStatusBar + Send→Stop; verified on-device. |
| **FRE-411: SLM telemetry joinable ✅** | 2026-05-28 | PR #101 + slm_server PRs. Trace headers + ES keyword index; 8/8 SLM calls join by span_id. |
| **FRE-404/P0 + FRE-402/398/410 ✅** | 2026-05-28 | PRs #92-100. Prompt corpus renderer (107 KB, 13 prompts); terminal tool short-circuit; classified error cards; `read` 200-line head cap + ranged paging (31K-token reduction on executor.py). |
| **FRE-396: Mermaid→SVG artifacts ✅** | 2026-05-28 | mmdc server-side render in `artifact_draft`; inline SVG; ADR-0070 D7 amended. |
| **FRE-392: WS duplicate guard ✅** | 2026-05-27 | PR #85. `MessageDeduplicator` (client_msg_id + SHA-256, 120s TTL). |
| **FRE-388: WebSocket transport ✅** | 2026-05-27 | PR #83 + 8 hotfixes. ADR-0075. SSE→WS; Postgres `session_events` replay; WS ticket auth. Verified live on iPad. |
| **FRE-387/385: eval isolation + Captain's Log ✅** | 2026-05-26 | PRs #81-82. eval_mode gate blocks consolidation→Neo4j; 3 CL files confirmed; 2-week promotion gate ~2026-06-09. |
| **FRE-375/374/376 (Phases 1-5): traceability ✅** | 2026-05-22–23 | PRs #69-80. Test substrate isolation (7688/9201/5433); cross-fact constraints (ADR-0073); 370+ identity-threaded log sites. |

*Older items → `docs/plans/completed/2026-05-22-completed-archive.md` · `docs/plans/completed/2026-05-10-completed-archive.md`*

---

## Active ADRs

| ADR | Title | Status |
|-----|-------|--------|
| **0090** | **Telemetry Surface Contract (emit↔mapping↔dashboard)** | **Proposed 2026-06-08 (PR #189). Governs the _Telemetry Surface Audit_ project (L0); three-corner reconciliation contract, report-only→gate CI phasing. Complements ADR-0088 (emission seam vs storage/display surface). Realization: FRE-533→539 + one D5 checker ticket (Needs Approval).** |
| **0089** | **Artifact Execution Security (sandbox not sanitize)** | **Implemented 2026-06-07 (509–512 live+verified). Addendum A merged 2026-06-08 (PR #188) — curated `/lib/` toolkit; impl FRE-526✅(PR #190)/527–532. FRE-525 umbrella In Progress.** |
| **0088** | **Execution Topology Observability Contract** | **Accepted 2026-06-06; spine shipped (FRE-513 PR #178) + read surfaces (514/515/519). Open: FRE-517 per-topology rows, FRE-518 live-render bug.** |
| **0084** | **Pedagogical Architecture: Socratic Tutor Layer** | **Accepted 2026-06-03 (PR #146). Primary = pedagogical continuity layer; delegation = bounded cognition only; 5-layer architecture; result-type taxonomy. Supersedes ADR-0082 D2–D5 for routing question. FRE-447/448/449 Done (M1). FRE-432 reconceived → Needs Approval; FRE-450 tracks scope revision.** |
| **0082** | **Tier-Aware Model Selection for SINGLE Tasks** | **Partially Superseded by ADR-0084 2026-06-03 — D2–D5 superseded for pedagogical routing; D1 plumbing may still ship in M4. FRE-432 scope invalidated → reconceived.** |
| **0081** | **Cache-Aware Context Layout & Compaction** | **Core chain COMPLETE + live: D1 ✅ (FRE-422) · D4 skill-index split ✅ (FRE-431) · D2/D3 frozen layout + scheduler ✅ (FRE-434, PRs #129/#130, enabled in prod). Deferred follow-ups tracked in project _ADR-0081 Extended — Context & Memory Injection Quality_: D4-index-trim (FRE-464), D5 cold-tier retrieval (FRE-465), D6 pin (FRE-466) — all Needs Approval.** |
| **0080** | **Thinking Control — server-side vs `/no_think` suffix** | **Implemented 2026-05-29 (FRE-417, PR #107)** |
| **0079** | **Server-Authoritative Session Profile** | **Implemented 2026-05-29 (FRE-416/419, PRs #102-104)** |
| **0078** | **Prompt Management & Observability** | **In Progress — P0 ✅ P1 ✅ P2 ✅ (FRE-406) P3 ✅ (FRE-407); P4 (FRE-408) next, P5 (FRE-409) after** |
| **0077** | **Artifact Draft — Sub-Agent HTML Generation** | **Implemented 2026-05-27 (PR #84)** |
| **0076** | **Adaptive Constraint Governance Protocol** | **Implemented 2026-05-28 (FRE-389, PRs #86-91)** |
| **0075** | **WebSocket Transport + Durable Channel** | **Implemented 2026-05-27 (FRE-388, PR #83 + 8 hotfixes)** |
| **0074** | **End-to-End Traceability + Identity Threading** | **Accepted 2026-05-30 (FRE-376 all phases ✅)** |
| **0073** | **Cross-Fact Constraint Layer** | **Proposed — FRE-374 replay ✅; D4 provenance pending perf probe** |
| **0070** | **Output Channel Model** | **Implemented 2026-05-21; D8 review gate ≥ 2026-06-04** |
| **0069** | **R2-Backed Artifact Substrate** | **Implemented 2026-05-17; FRE-369 next consumer** |
| **0067** | **Reflection Surfacing in Context Assembly** | **Accepted; eval → FRE-350** |
| **0066** | **Skill Routing Defaults + Threshold** | **Accepted; Wave J eval complete** |
| 0065 | Cost Check Gate — Atomic Reservation | Accepted + Implemented; FRE-311 parked |
| 0061 | Within-Session Progressive Context Compression | Accepted + Implemented (FRE-251) |
| 0060 | Knowledge Graph Quality Stream | Accepted + Implemented; gate live |
| 0052 | Seshat Owner Identity Primitive | Accepted + Implemented |
| 0041 | Event Bus — Redis Streams | Accepted; Phases 1–4 live |
| 0040 | Linear as Async Feedback Channel | Accepted; Phases 1–2 live; Phase 3 → FRE-183 |

*Full ADR list: `docs/architecture_decisions/`*

---

## How This File Works

- **Linear is the task tracker** — this file tracks priorities and sequencing only.
- **Next task**: pick from "Immediately Actionable" table above, highest priority first.
- **Update after every ship**: add to Recently Completed, bump Last updated.
- **Specs** → `docs/specs/` · **ADRs** → `docs/architecture_decisions/` · **Session plans** → `docs/superpowers/plans/`
- **Archive** → `docs/plans/completed/` (items older than ~1 week)
