# Cost governance model — rework of ADR-0065 (decision note)

**Date:** 2026-07-16
**Author:** cc-explore2 (ephemeral seat) **in conversation with the owner** — collaborative design session, not a solo harvest.
**Status:** Decision-ready. Compiled for **master** to route → ADR (reworks/supersedes ADR-0065) + implementation tickets. This note itself is a draft staged in the explore2 worktree; **master commits it to main via PR** (main requires PR + green checks).
**Scope note:** this started as a narrow "per-feature lanes → per-provider pool" question. The owner's design session moved it well past that, to a **governance philosophy shift** (enforcement → visibility + consent). This note supersedes its own earlier provider-pool framing; that framing survives only as one input (§5 grouping).

**Inputs:** `docs/architecture_decisions/ADR-0065-cost-check-gate.md`; `src/personal_agent/cost_gate/{gate,policy,types,__init__}.py`; `config/governance/budget.yaml`; `config/model_roles.yaml`, `config/models.cloud.yaml`, `config/profiles/cloud.yaml`; `src/personal_agent/memory/{embeddings,reranker}.py`; `src/personal_agent/tools/perplexity.py`; `src/personal_agent/second_brain/consolidator.py`; `src/personal_agent/transport/agui/transport.py`; `src/personal_agent/service/ws_ticket.py`; live vendor pricing pages (§4).

---

## TL;DR

**Kill hard, process-breaking dollar caps.** They can't tell "using it a lot" from "it's broken," and a hard cap is exactly what caused the ADR-0065 incident (the $10 weekly cap tripped → every call raised → empty PWA turns). Replace enforcement with **forward-by-default + visibility + owner-consent**:

- **Frontend (user-facing):** **approval cards** — Claude-Code-style approve/deny. Below threshold, silent forward; above it, a card. Forward unless the owner stops it. *(The card primitive already exists — `request_tool_approval` renders one today for tool approval.)*
- **Backend (background):** **no hard stop.** Detect anomalous *shape* (spend rate/fan-out, not a dollar ceiling) → **pause** the offending role (reversible; nack-roles just retry later, nothing breaks) → **alert the owner** → owner is the **only** authority that resumes or kills. Automation may *slow*, never *terminate*.
- **Pause is the safety primitive** that makes "no hard limit" safe even at 3am with no human watching: it holds spend flat until a human answers, through whichever channel reaches them.
- **Step zero (mandatory, agreed):** instrument **OVH embedding** + **Voyage reranker** into `api_costs`. They are metered, token-priced, and **completely off the books today** — no ledger row, no cap, no visibility. You cannot alert on, or gate, a vendor you don't measure.

---

## 1. The governance shift (the core decision)

Hard caps conflate two unrelated failures:
1. **Runaway / anomaly** — a bug, a loop, a fan-out storm. Unbounded, fast, unintended.
2. **Sustained normal-but-expensive use** — you're just using it a lot. Bounded by intent.

A dollar ceiling punishes both identically by breaking the process. The redesign separates them:
- **Normal use → never stop.** Indicate, and let the owner consent to spend (approval cards) or review it after (alerting).
- **Runaway → an automatic *reflex*, but a soft one.** Pause + alert, keyed on **shape** ("8× normal rate", "N reservations for one trace in M seconds"), not on an absolute cap. It catches the *bug* and is blind to you using the thing heavily.

**Two verbs, deliberately split:**
- **Pause** = reversible, self-healing throttle. *Automation may do this.* Not "process-breaking": for a background nack-role, pause = "finish current item, then hold; resume later" — the work is deferred, never lost or errored.
- **Stop / kill** = terminal. **Owner-only.** The automation's authority ends at "slow down and ask."

**The honest accountant's caveat (owner accepted, eyes open):** "no hard limit" trades a small *guaranteed* annoyance (occasional false-deny) for a small *probability* of a large bill from a bug caught late. The pause bounds the *rate* of loss, not loss-to-zero — some money is spent between the trip and the owner's response. For a single-user research system with known, modest vendors, this is the right trade — but it is a trade, and it is only as good as the measurement under it (→ step zero).

## 2. The mechanism (grounded in what already exists)

**a. Pause — proven pattern, needs a uniformity audit.**
The consolidator *already pauses itself* mid-loop today: `should_pause()` callback → `consolidation_paused_request_active` → `while should_pause(): sleep(1)` → `consolidation_resumed` (`second_brain/consolidator.py:159,220`). The owner's intuition is confirmed and the reason it's safe is exact: **the pause boundary is *between* items, and both datastores commit per-item**, so a pause never catches a half-written Neo4j/Postgres transaction; Redis Streams makes it cleaner still (stop consuming → unacked messages redeliver). **Caveat:** the pattern is per-consumer and cooperative, not a system-wide switch — only the consolidator honors it today. **Audit required** (per background role: entity_extraction, captains_log, insights, promotion, freshness): (a) is there a safe between-items boundary, (b) does its loop poll/honor one shared pause signal there, (c) can a single shared state (e.g. a Redis key per role) drive them all.

**b. Frontend approval card — already built.** ADR-0075 WebSocket transport already does server→PWA push, and **`request_tool_approval` already renders an approval card and blocks on the human's verdict** (`transport/agui/transport.py:375`) — Claude-Code approve/deny, shipped, for tool approval. The **cost** approval card is the same primitive with a different payload — *not* new infrastructure.

**c. The no-socket gap → why the phone channel is required, not optional.** The WebSocket is **session-scoped** (`(user_id, session_id)`, `service/ws_ticket.py`). It pushes to a *connected* PWA. The code even tells us the degrade path: no active socket → `register_and_push_constraint` returns a default with `resolution="connection_lost"` (`transport/agui/transport.py:164`). So the PWA card works **when the owner is watching** and degrades to nothing **when they aren't** — and the 3am runaway is exactly the no-socket case. Hence:
> **PWA card = in-band** (connected). **Signal/phone = out-of-band** (asleep). **Pause = the reflex that buys time for either channel to reach the owner.**

**d. Anomaly detection — substrate exists.** The insights pipeline already fires cost-spike patterns (FRE-870 "Cost spike detected", FRE-629 "entity_extraction_spike"). The signal is produced; it is not yet wired to the pause reflex. The tuning problem is real and named: a false trip pauses real work; a missed trip lets spend through — the price of trading the crude-but-certain hard cap for shape-detection.

**e. Alerting host + channel (owner-decided):**
- **Alerting runs on the VPS.**
- **Out-of-band channel: Signal via `signal-cli`** — self-hosted, end-to-end, no third party, on-brand with the owner's custody/privacy posture (ADR-0111). **Email/SMTP as the dumb-but-reliable fallback** for when the Signal bridge itself is down.
- Seshat has **no outbound owner-notification channel today** (verified: the iMessage/Signal the owner uses is a Claude-Code *harness* skill, not a Seshat capability) — this is net-new build.

## 3. Full cost-surface inventory (every place Seshat spends)

| Spend surface | Serves | Model → Provider | Gated today | In `api_costs` | Denial |
|---|---|---|---|---|---|
| `main_inference` (`primary`) | The live user turn | Sonnet → Anthropic | cap | ✅ | raise |
| `sub_agent` (HYBRID) | Concurrent sub-agents on a turn | Haiku → Anthropic | folds → `main_inference` | ✅ | raise |
| Artifact building | `artifact_tools.py:1437` → `get_llm_client("sub_agent")` | Haiku → Anthropic | **hidden inside `main_inference`** | ✅ | raise |
| Vision ingestion | attachment/PDF on the turn (`executor.py:3633`) | primary/escalation → Anthropic | **hidden inside `main_inference`** | ✅ | raise |
| `skill_routing` | pre-turn skill selection | small → cloud | cap ($0.10/day) | ✅ | raise |
| `entity_extraction` | KG writer (bg) | gpt-5.4-mini → **OpenAI** | cap | ✅ | nack |
| `captains_log` | reflection writer (bg) | Sonnet → Anthropic | cap | ✅ | nack |
| `insights` | pattern writer (bg) | Sonnet → Anthropic | rides `_total` only | ✅ | nack |
| `promotion` / `freshness` | KG lifecycle (bg) | declared, low volume | **no cap row** (`_total` only) | ✅ | nack |
| `study` | ADR-0114 one-shot ingest | isolated | cap | ✅ | raise |
| **Embedding** | KG + recall vectors | Qwen3-8B → **OVH** | ❌ **none** | ❌ **UNTRACKED** | — |
| **Reranker** (+`_fallback`) | recall ranking | rerank-2.5 → **Voyage** | ❌ **none** | ❌ **UNTRACKED** | — |
| **Perplexity** | web-synthesis tool | paid/query | ❌ **none** | ❌ **UNTRACKED** | — |
| Deep research | *does not exist yet — proposed* | deterministic fan-out | — | — | — |
| Web search | SearXNG (self-hosted) | — | free | n/a | — |

Three findings from this table drive the design: (1) **three metered vendors off the books** (OVH, Voyage, Perplexity); recall fans the reranker out (candidate-count × query) so a recall-heavy turn is a silent cost multiplier. (2) **Artifact + vision bill invisibly** under `main_inference` — you can't see what they actually cost. (3) The model conflates "who to protect from starvation" (denial semantics) with "which vendor am I exposed to" (accounting); a good design keeps them separate.

## 4. Vendor pricing (both token-based — confirmed by owner + citations)

- **Voyage `rerank-2.5`:** **$0.05 / 1M tokens** ($0.00005/1K). First 200M tokens/account free. Rerank tokens = `(query_tokens × num_documents) + Σ(document_tokens)` — the whole candidate set counts, not just the query. Source: <https://docs.voyageai.com/docs/pricing>.
- **OVH `Qwen3-Embedding-8B` (AI Endpoints):** **€0.10 / 1M input tokens**, no output-token charge. Source: <https://www.ovhcloud.com/en/public-cloud/ai-endpoints/catalog/>.
- **Currency wrinkle for the ledger:** OVH bills **EUR**, everything else + `budget_policies.cap_usd` is **USD**. Onboarding OVH means a currency conversion step or a native-currency column — new plumbing the current `*_usd`-named schema does not have.

## 5. Grouping for management + the config UI (three tiers)

Organizing principle (accountant + CTO): **gate/alert at the granularity of a unit of work a human initiates, not per model call.**

**Section A — Substrate / "keeping the lights on" (backend pinned models).** Pinned (user never picks them), always-on, supporting a substrate component. Grouped **by component:** *KG writers* (entity_extraction, captains_log, insights, promotion, freshness) · *Retrieval substrate* (embedding→OVH, reranker+fallback→Voyage — the untracked ones). Shared: invisible denial (nack/retry), opex. **Manage as per-vendor alerting thresholds + per-role visibility** — enforce/alert at the ~4 vendor pools you're actually invoiced at (Anthropic-bg, OpenAI-bg, OVH, Voyage), keep per-role telemetry in the UI so you can *see* the breakdown without maintaining a wall per role. Containment *within* background isn't worth per-role walls; the only firewall that matters is A-vs-B, which preemption handles. This is where "pinned models are all the same" is genuinely right.

**Section B — Interactive / the live path (what a user waits on).** Conversation orchestrator (primary + sub_agent), skill_routing, vision-on-turn. Denial visible (raise). **Priority: the live turn always wins** (preemption, §6). Never paused; gets an approval card instead when a spend needs consent.

**Section C — Discretionary heavy jobs (explicitly-invoked, bounded, expensive).** Artifact building, **deep research**, study/one-shot ingest, Perplexity. A human kicks off one unit that spawns many calls. **Gate at the *job envelope* (the "purchase-order" model):** reserve the whole estimated budget up front (one PO), let sub-calls settle against it — the existing `reserve()`/`commit()`/`refund()` primitive applied at *job* scope instead of *call* scope. **Ask-me-first:** over threshold, estimate → owner confirms → run. Determinism is what makes this honest: a fixed fan-out (K searches × M tokens) is pre-budgetable; an LLM-router deciding dynamically is not — which is why deep research should be a **deterministic workflow** (as Claude Code does it), not a model-in-the-loop router.

## 6. Settled decisions (this session)

1. **No hard, process-breaking dollar caps** anywhere. Enforcement → visibility + consent.
2. **Backend = pause + alert** (option B: auto-pause-and-ask, reversible). Anomaly on *shape* → pause role → alert owner → **owner is sole resume/kill authority.** (This supersedes the earlier "reserved floor / preemption-vs-fixed-floor" question — under no-hard-limits there is no dollar floor; **priority preemption** is the ordering rule for the live path, and pause+alert is the safety, not a carve-out.)
3. **Frontend = approval cards**, forward-by-default, owner stops it. Reuses the shipped `request_tool_approval` primitive.
4. **Section C jobs = job-envelope reserve + ask-me-first.**
5. **Section A = per-vendor alert thresholds + per-role visibility.** (Owner asked for the pros/cons; this is the standing recommendation — confirm at ADR.)
6. **OVH + Voyage → `api_costs` = mandatory step zero** (prerequisite for gating *and* alerting).
7. **Alerting runs on the VPS.** Out-of-band channel = **Signal (`signal-cli`)**, email fallback; in-band = PWA card over the existing WebSocket.
8. **Deep research (when built) = deterministic workflow**, gated at the job envelope.

## 7. Open questions (for the ADR / master — not yet decided)

- **Pausability audit** (§2a): per-consumer between-items check-points honoring one shared pause signal. This is a real, scopeable ticket and a prerequisite for the backend reflex.
- **Anomaly-detector tuning:** what shape trips the breaker; reuse the insights cost-spike pipeline (FRE-870/629) vs a purpose-built rate detector; false-trip vs missed-trip calibration.
- **Approval-card threshold:** fixed-dollar vs anomaly-shaped ("this turn/job is unusually expensive vs your normal"). Undecided.
- **Currency handling** (§4): conversion step vs native-currency ledger column for EUR (OVH).
- **Resume/kill round-trip over Signal:** delivery guarantee, dedupe, and how a "[resume]/[keep paused]/[kill]" action from a phone actuates back into the VPS pause-state. (Notifications must be actionable — reply drives state.)
- **Perplexity:** onboard into `api_costs` + Section C, or retire in favor of the deterministic deep-research workflow?

## 8. Handoff to master (draft shapes — master executes; explore does not file)

**Recommended route:** this reworks ADR-0065 substantially (a philosophy change, not a tweak) → **new ADR that supersedes/amends ADR-0065**, authored in the adr seat with the owner. Then implementation tickets under it. Proposed decomposition, foundation-first:

- **T0 (prerequisite) — Instrument OVH + Voyage into `api_costs`** (+ Perplexity). Includes the EUR/USD currency decision. *Nothing else is real until spend is visible.*
- **T1 — Pausability audit** across background consumers; define the one shared pause-signal (Redis key per role) + per-consumer check-points.
- **T2 — Anomaly → pause reflex** on the VPS: wire the insights cost-spike signal (or a purpose-built rate detector) to the pause-signal; owner-only resume/kill.
- **T3 — Out-of-band alerting:** `signal-cli` on the VPS + email fallback; actionable payload (resume/keep-paused/kill) that actuates back to pause-state.
- **T4 — Cost approval cards (frontend):** extend the shipped `request_tool_approval` primitive to a cost-consent payload; Section-C ask-me-first threshold.
- **T5 — Split artifact_builder + vision out of `main_inference`** for visibility (coordinate with ADR-0118/0119 which already extract `artifact_builder`).
- **T6 — Config-UI cost surface:** the three-tier grouping (§5) rendered in the ADR-0119 config interface — per-vendor thresholds + per-role telemetry.
- **Deep research** is a separate feature project (deterministic workflow); its cost model = Section C job-envelope. File under its own ADR when it's picked up, referencing this one.

---

## References

- ADR-0065 (cost gate — reworked by this note) · ADR-0075 (WebSocket transport) · ADR-0111 (data custody/privacy posture) · ADR-0118/0119 (artifact_builder extraction + config interface)
- Code: `cost_gate/{gate,policy,types,__init__}.py` · `config/governance/budget.yaml` · `config/model_roles.yaml` · `second_brain/consolidator.py:159,220` (pause pattern) · `transport/agui/transport.py:164,375` (approval card + connection_lost) · `service/ws_ticket.py` (session-scoped WS) · `memory/{embeddings,reranker}.py` (untracked vendors) · `tools/perplexity.py` (untracked)
- Signals: FRE-870, FRE-629 (cost-spike anomaly patterns already firing)
- Pricing: Voyage <https://docs.voyageai.com/docs/pricing> · OVH <https://www.ovhcloud.com/en/public-cloud/ai-endpoints/catalog/>
- Provenance: entity_extraction→OpenAI was a **deliberate 2026-03-28 pricing decision** (commit `694ed218`, ~93% cost cut), not incidental — the cost-gate isolation it happens to give is a byproduct of that pricing choice, not a defended invariant (which is *why* the reserved-floor/priority logic should key on denial-semantics, not on which provider a role currently sits on).
