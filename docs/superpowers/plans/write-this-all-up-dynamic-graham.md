# Decomposition First-Run — Write-up, Tickets & PR Plan

## Context
On 2026-06-06 the artifact-decomposition A/B switch (`artifact_decomposition_enabled`, ADR-0086 / FRE-480/481) was flipped **on** and exercised on a real complex artifact build (trace `87cbd720`). Outcome: a **brilliant artifact at lower cost** than the old 20+-round single-agent path — the quality + cost case for decomposition is made, and it **stays on**. But the run surfaced four concrete gaps (memory grounding, cost meter, live status, planner reliability) plus the owner's pending request to disable the artifact `<script>` sandbox check. This plan turns the findings into a documented record, a clean set of Linear tickets, and a sequenced set of code PRs — answering "do we need a PR for any code?" (yes: four, sequenced).

Owner decisions (this session): memory grounding → **Both** (quick bump now + design under FRE-435); sandbox → **flag-gated but treated as a temporary bridge** pending a deeper artifact-architecture discussion; decomp PRs → **cost+status first, planner next**.

## Already done
- Research record: `docs/research/2026-06-06-decomposition-first-run-findings.md` (+ README index) — pushed `094ec0c`.

## Investigation discipline (applies to EVERY ticket — non-negotiable)
The root causes below are **this session's hypotheses**, derived from a single trace (`87cbd720`) plus a code read. They are **NOT to be taken as given.** Every ticket's **first task is to independently confirm its hypothesis** with logs + telemetry — ES `agent-logs-*` / `agent-captains-captures-*`, the cited code paths, and a fresh repro where useful — **citing `trace_id` / `event_type` / `file:line`, and correcting the hypothesis if the evidence disagrees**, before writing any fix. Measure-don't-assert (the FRE-433/434 methodology). Each ticket's acceptance criteria therefore lead with a **Confirm** step → then Implement → then Verify. A ticket that implements against an unconfirmed hypothesis is incomplete.

## Ownership (three-session split)
- **Master (this session):** create/update tickets, update MASTER_PLAN, push the research doc (done), and **seed the adr-session architecture conversation** (kickoff pointer). Integrate each PR at the gate (review → owner-approved deploy → verify → close).
- **Build session:** implement PR-1 → PR-4 from the Approved tickets; stop at PR.
- **adr session:** run the Architecture Review conversation (all seven threads) → ADR(s).

## Work items

### Write-up (docs, direct to main)
- **MASTER_PLAN update:** record decomposition rolled out + live (flag on), the four gaps with their tickets, and that compression stays parked. Bump "Last updated."

### Tickets (create / update)
| Ticket | Action | Needs PR? | Tier |
|---|---|---|---|
| **A — Decomp observability: sub-agent cost roll-up + live `turn_status`** | create (Approved) | **PR-1 (first)** | Sonnet |
| **B — Decomp planner reliability: schema-validation recovery + discovery-aware fallback** | create (Approved) | **PR-2 (next)** | Opus |
| **C — Memory grounding for build/teach: proactive depth + recall-gate extension** | create (Approved) | **PR-3** | Sonnet |
| **FRE-500 — Sandbox `<script>` enforcement: flag-gate (default off)** | update → Approved; note "temporary bridge, likely removed after architecture discussion" | **PR-4** | Sonnet |
| **FRE-435 — Memory-Recall Quality (In Progress)** | comment: add the build/teach pedagogical case + cross-session KG-depth design as a contribution (the "design" half of "Both") | design/measure, no PR yet | Opus |
| Artifact-sandbox architecture discussion | add as a comment/relation on **FRE-397** (dynamic-artifact tiers) — owner flagged the whole strip-and-deliver model as likely-wrong; track the discussion there rather than a new ticket | discussion, no PR | — |

Keep ticket creation minimal (3 new + 2 updates + 1 note) — no sprawl.

## The code PRs (sequenced; build session implements, master integrates)

### PR-1 — Decomp cost roll-up + live status (Ticket A) — FIRST
**Hypothesis (confirm first):** the meter showed `$0.57` (primary only) vs `$0.90` true, and stayed blank during the ~13-min decomposition, because `ctx.turn_cost_usd` only sums the primary loop and the expansion path never aggregates sub-agent cost or calls `_emit_turn_status`.
**Confirm (telemetry):** on a decomposition trace, sum `model_call_completed.cost_usd` by `role` (primary vs sub_agent) and check it against the `api_cost_recorded` sum **and** the `turn_cost_usd` carried on the emitted `turn_status`; confirm there are zero `turn_status` emissions during the expansion window. Read `executor.py:2667` + the two expansion sites to confirm the missing aggregate/emit. Only proceed if confirmed.
**Reuse:** `_emit_turn_status(ctx)` already exists (`orchestrator/executor.py:190-217`); cost-accumulate pattern `ctx.turn_cost_usd += response.get("cost_usd")` already used at `executor.py:2667`.
**Changes (~30 lines, 4–5 files):**
- `orchestrator/sub_agent_types.py:55-83` — add `cost_usd: float = 0.0` to `SubAgentResult`.
- `orchestrator/sub_agent.py` — extract `response.get("cost_usd")` from each LLM call (`_run_tooled_loop` ~320-329 & ~423-431; default call ~127-159), accumulate, set on the returned `SubAgentResult`.
- `orchestrator/expansion_controller.py` — capture planner-call cost (~260-273); sum sub-agent costs; expose `cost_usd`/`planner_cost_usd` on `ExpansionResult`.
- `orchestrator/executor.py` — at the two expansion sites (enforced ~1721-1769, autonomous ~2706-2750): accumulate sub-agent+planner cost into `ctx.turn_cost_usd`, then call existing `_emit_turn_status(ctx)`. Add a progress `_emit_turn_status` at dispatch start so the meter lights up *during* decomposition, not only at the end.
**Tests:** unit — `SubAgentResult.cost_usd` populated; an expansion turn rolls sub-agent cost into `ctx.turn_cost_usd`; `_emit_turn_status` called post-expansion.

### PR-2 — Decomp planner reliability (Ticket B) — NEXT
**Hypothesis (confirm first):** `planner_failed: schema_validation_failed` → fallback planner emits `mode=PARALLEL_INFERENCE, tools=[]` → discovery sub-agents run **tool-less** (the "no web search / no real discovery" we saw).
**Confirm (telemetry):** on the trace, confirm `planner_failed reason=schema_validation_failed` then `fallback_planner_used`; confirm sub-agents dispatched with empty tools (no discovery `tool_call_started` inside sub-agents). **Crucially, pull the actual planner LLM output that failed** (from `agent-captains-captures-*` / `llm_call_messages_debug`) to see *which field* failed validation — the fix depends on whether it's malformed JSON, a missing field, or a mode/tools mismatch. Correct the hypothesis if the real failure differs.
**Changes:**
- `orchestrator/expansion_controller.py:470-541` (`_validate_plan_json`) — stop being all-or-nothing: emit *which field* failed; add graceful partial-parse (keep valid tasks, drop malformed fields) instead of returning `None`; optional one planner retry with simplified guidance before fallback.
- `orchestrator/fallback_planner.py:49-202` (`generate_fallback_plan`) — make it **discovery-aware**: when `artifact_decomposition_enabled`, emit `TOOLED_SEQUENTIAL` tasks with a default discovery tool set (e.g. `["bash","read"]`) so fallback sub-agents still run discovery (and can hit memory/web).
- Telemetry: distinguish "planned discovery" vs "fallback inference-only" so degraded runs are visible.
**Tests:** malformed planner output → recovers partial plan or discovery-aware fallback (not tool-less); a fallback turn still dispatches `TOOLED_SEQUENTIAL` sub-agents with tools.

### PR-3 — Memory grounding for build/teach (Ticket C; "quick" half of Both)
**Hypothesis (confirm first):** build/teach (`TOOL_USE`) gets only shallow proactive grounding; deep cross-session pull never happens (recall gated to `CONVERSATIONAL`).
**Confirm (telemetry) — this is the crux, do NOT assume:** on the trace, confirm `recall_controller_skipped` (`original_task_type=tool_use`). Then **inspect what proactive memory actually surfaced** — how many candidates, and were they genuinely the user's prior spectral-vision discussions or generic memories? Pull the injected `memory_context` from the captures and **join the artifact's references back to KG entities/turns** to test whether the grounding was real-but-shallow vs. effectively absent. This determines whether the lever is "deepen proactive" or "proactive isn't retrieving the right things at all" — the fix differs. Feed the finding into FRE-435.
**Key nuance (from exploration):** `recall_controller` scans the **current session only** — it is *not* the cross-session KG lever. The cross-session "subject discussed before" grounding comes from **proactive** memory (Neo4j vector). So the high-value quick win is **deeper proactive for build/teach**, with the recall-gate extension as a smaller session-scoped add.
**Changes:**
- Proactive depth (the real lever): `request_gateway/context.py:179-190` / `memory/service.py:258` — raise `proactive_memory_vector_top_k` (new `_build_multiplier` setting) when `intent.task_type == TOOL_USE` and `"artifact_build" in intent.signals`.
- Recall-gate extension (session-scoped add): `request_gateway/recall_controller.py:172` — `not in (CONVERSATIONAL, TOOL_USE)` gated by a knowledge-building sub-signal (`artifact_build` + teach/learn keyword). Reuses the existing 3-gate design; session-fact scan is already capped/safe.
**Tests:** a build/teach `TOOL_USE` request triggers deeper proactive top-k and (with cue) the recall path; plain `TOOL_USE` (non-teach) unchanged.
**Design half (no PR):** comment on **FRE-435** — this is a concrete pedagogical instance of the recall-quality gap; the durable fix (cross-session KG retrieval depth/targeting for knowledge-building) should be measured + designed in that program (ADR-0087, ties to ADR-0084 pedagogical bar).

### PR-4 — Sandbox `<script>` enforcement flag (FRE-500) — TEMPORARY BRIDGE
**Hypothesis (confirm first):** the four enforcement points hard-fail/strip interactive `<script>` artifacts and the system prompt steers the model off JS. Owner: ship them now; flag is a bridge, the whole model likely gets redesigned after the architecture discussion.
**Confirm (telemetry/repro):** drive an interactive artifact request and confirm via logs which point actually fires (`validate_reject` TerminalToolError / `artifact_draft_sandbox_retry` / `artifact_draft_sanitized_sandbox_violations`), and that the model was steered to CSS-only by the system prompt. Confirm the four points are the complete enforcement surface (no other path strips/blocks). Then build the flag.
**Changes (`tools/artifact_tools.py`):** add setting `artifact_sandbox_enforcement_enabled` (default **False**); gate all four points on it —
1. `_validate_html_output` raises (~1227-1240) — skip on flag-off;
2. CSS-only retry (~1390-1407) — skip on flag-off;
3. strip-and-deliver `_sanitize_sandbox_violations` (~1536) — pass-through on flag-off;
4. `_HTML_GENERATION_SYSTEM_PROMPT` (~783-794) — flag-off variant that **allows/encourages interactive JS** (essential, or the model keeps emitting CSS-only).
- Remove the throwaway debug WIP (`_debug_dump_sandbox`, `_DRAFT_ATTEMPT_COUNTS`).
- Code marker + FRE-500 note: temporary, default may be removed/redesigned per the FRE-397 architecture discussion; keep malformation checks (DOCTYPE/`</html>`/min-length).
**Tests:** flag-off → script artifact ships intact (no raise/strip), JS-allowed prompt; flag-on → existing FRE-496 behavior preserved.

## Disposition / housekeeping
- **`.env` decomp flag:** stays ON (decomposition enabled).
- **FRE-500:** keep — it's the right tracker for PR-4 (re-scoped to flag-gate, temporary).
- **Debug WIP in `artifact_tools.py`:** removed in PR-4.
- **Deploys:** PR-1/PR-4 affect live behavior → owner-approved deploy at the gate, **not while a build session is mid-turn** (decomposition turns run ~13 min). PR-2/PR-3 likewise.

## Conclusion correction (owner, 2026-06-06)
Decomposition-with-subs **does not work yet** — the brilliant artifact came from Sonnet + the artifact pipeline, *not* from working decomposition (planner failed → tool-less generic subs; memory 10→2; gate bypassed; meter lied; UI blind). The first run was a **successful probe** that exposed a 6-issue cluster. **Flag stays ON; we iterate to working.**

## Sequencing — VISIBILITY-FIRST (owner principle: we iterate fast only when we can *see* what the model and gates do)
**Every ticket carries a visibility deliverable** — not just a fix. Then each wave is verified by *watching telemetry*, not forensics.

- **Wave 0 — SEE (do first; unblocks everything):**
  - **FRE-501** — live cost roll-up + `turn_status` during expansion (user-facing meter).
  - **FRE-505** — sub-agent **input context + output + injected digest** auditable (engineering visibility — answers "what was each sub fed/did/returned").
  - **FRE-506** — sandbox **gate-decision telemetry** (`pass`/`reject`/`strip`/`bypassed`) + confirm the bypass path.
  - → Deploy Wave 0, then **every subsequent fix is observable live.**
- **Wave 1 — MAKE IT REAL (root cause):** **FRE-502** — planner schema-validation recovery + discovery-aware fallback (so subs actually run tool-using discovery, not generic tool-less). Verified via Wave-0 visibility.
- **Wave 2 — GROUND IT:** **FRE-503** — proactive depth for build/teach (raise the 500-tok budget that trimmed 10→2) + recall-gate extension; feed measurement to FRE-435.
- **Parallel lane (independent files):** **FRE-500** — flag-gate the sandbox off (interactive artifacts; temporary bridge, paired with FRE-506).
- **adr (ongoing):** **FRE-504** — the 7 architecture threads → ADR(s).

Loop after each wave: deploy → run one decomposition build → **watch** (cost meter, sub-agent records, gate decisions, planner planned-vs-fallback, memory surfaced) → iterate. Owner-approved deploys; not while a build session is mid-turn.

## Architecture Review (open conversation — feeds ADRs, not a code ticket)
The first decomposition run was a *probe* that exposed deeper structural questions. These are for a free architectural discussion (owner + adr session), likely producing one or more ADRs:

1. **Observability is coupled to single-agent execution.** The entire live surface (`turn_status`, cost, token meter) emits from the executor's *primary* loop (`executor.py`); the moment work moves into sub-agents it goes dark and under-counts. → Should the live surface + cost be a **cross-cutting concern** emitted at a layer every execution topology passes through (primary, sub-agent, future planner-executor), not bolted to one loop?
2. **Memory retrieval is gated on task-type — which fights the pedagogical north star.** Deep recall is `CONVERSATIONAL`-only; "teach me about X" is `TOOL_USE`, so the requests that most need grounding are structurally denied it. → Should grounding be driven by **content (knowledge-building intent)**, not **output shape (artifact vs sentence)**? Is task-type the wrong axis?
3. **Write/read asymmetry.** The turn *wrote* 21 entities to the KG and *read* ~none. The system accumulates knowledge it doesn't leverage. → Is the retrieval path under-invested vs. the extraction path? (the heart of FRE-435 / ADR-0087).
4. **Silent degradation.** Planner schema-fail → tool-less fallback → the turn "succeeded" while doing something lesser, invisibly. → Should degradation be **loud**? How robust should the planner be vs. how graceful the fallback?
5. **Artifact security/execution model.** Strip-and-deliver fights the model (generate JS → strip it); owner flags it as likely-wrong. → Move from "**sanitize output**" to "**sandbox execution**" (FRE-397 tiers: SVG → sandboxed-JS → JSX)? This is the discussion the FRE-500 flag is a bridge for.
6. **Capabilities outpacing their integration.** Decomposition shipped with backend joinability but no live surface, no memory integration, silent degradation. → What is the **"done" bar** for a new orchestration capability — should observability + grounding + loud-degradation be part of shipping it, per "observable-first"?
7. **Decomposition as a general pattern.** It worked and was cheaper. → Where else does the isolated-context worker / planner-executor split apply (cf. FRE-401)? Is decomposition a special case of a broader pattern worth naming?

**Owner decision: all seven threads go to a dedicated `adr`-session "tree" conversation** — not master, not a code ticket. The adr session works through them as a free architectural discussion and produces ADR(s), using this section + `docs/research/2026-06-06-decomposition-first-run-findings.md` as the seed. The PRs above are the tactical fixes; **this is the strategic layer the adr session owns.** Master seeds it (a short kickoff note/issue pointing the adr session at this plan + the research doc); the conversation itself happens there.

## Verification (end-to-end)
- After PR-1 deploy: run a decomposition artifact build; confirm the PWA meter lights up *during* the turn and the final cost ≈ the `api_cost_recorded` sum (e.g. ~$0.90, not ~$0.57). Cross-check ES: sum `model_call_completed.cost_usd` for the trace == meter cost.
- After PR-2 deploy: a decomposition build shows `tool_call_started` for discovery tools inside sub-agents (bash/read/search), no `planner_failed`-then-tool-less degradation; telemetry shows "planned discovery."
- After PR-3 deploy: a build/teach request shows deeper proactive suggestions (more KG entities/cross-session turns injected); spot-check the artifact references against the user's actual prior discussions.
- After PR-4 deploy: an interactive request ships a `<script>` artifact intact (no TerminalToolError, no strip banner); flag-on restores the FRE-496 guard.
- Per-PR: `make test` / `make mypy` / `make ruff-check` clean; master runs the live check at the gate.
