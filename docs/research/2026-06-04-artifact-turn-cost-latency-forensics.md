# Artifact-Turn Cost & Latency Forensics — Research & Assessment

> **Date:** 2026-06-04
> **Trace under the microscope:** `a0a07227-121b-4ccb-871b-45072a32ccb0` (2026-06-04 **10:10 local / 08:10 UTC**)
> **Model:** `claude-sonnet-4-6` (cloud path, `cloud-sim-seshat-gateway`)
> **Origin:** first successful artifact-build turn after the FRE-469 classifier fix
> **Tickets spawned:** FRE-475 · FRE-476 · FRE-477 · FRE-478 (project: *Turn Cost & Latency Optimization (artifact builds)*)

---

## 1. Abstract

FRE-469 fixed a classifier misroute: requests that ask the agent to *build an artifact* (e.g. "explain the internals and build an interactive HTML guide") were being classified `conversational`, whose tool-iteration budget is capped at **6** — so the agent exhausted its runway on discovery and never built anything. After the fix, build/artifact intent routes to `tool_use` (budget **25**) and the artifact ships.

This document is the **post-fix forensic pass** on the first turn that succeeded. The turn is *correct* but *expensive and slow*: **23 LLM rounds, ~$1.14, 14 min 34 s wall-time, 768 k full-price input tokens**. Measuring it round-by-round surfaces two structural causes — an **uncached, re-billed context tail** and a **generation tail that dominates wall-time and hits the output-token cap** — plus a latent design constraint (`TOOL_USE` pins complexity to `SIMPLE`, which blocks decomposition). We assess three optimization levers the owner proposed — **decomposition, advanced bash scripting, output compression** — against the measured data, rank them by ROI-to-risk, and translate them into four scoped tickets.

The method is the same one used for FRE-433/434: **measure, don't assert**; build a hypothesis around evidence; make every lever independently A/B-measurable with the per-round token curve.

---

## 2. The turn under the microscope

The originating bug (`c216bd40`, 04:33 UTC / 06:33 local) classified `conversational`, confidence 0.7, `signals=['no_special_patterns']`. Post-deploy (~07:30 UTC) every equivalent turn classifies `tool_use`, confidence 0.8, `signals=['tool_intent_pattern']`. `a0a07227` is the first such turn that ran to completion.

**Lifecycle (event histogram, `agent-logs-*`):**

| Event | Count | Reading |
|---|---:|---|
| `model_call_completed` | 23 | 23 LLM rounds |
| `tool_call_started` / `completed` / `failed` | 32 / 30 / 2 | heavy tool use, 2 tolerated failures |
| `bash_started` / `bash_completed` | 20 / 20 | shell-driven discovery |
| `read_executor_called` | 9 | file reads |
| `tools_dispatched_parallel` | 20 | tools *within* a round already parallelize |
| `artifact_draft_start → ..._sub_agent_complete → artifact_write_committed` | 1 each | **artifact shipped** ✅ |
| `entity_created` / `relationship_created` | 16 / 16 | memory pipeline ran (not degraded) |

**Aggregate economics (`api_cost_recorded` / `model_call_completed`):**

| Quantity | Value |
|---|---:|
| Wall-time (first→last event) | 08:10:48 → 08:25:22 UTC = **874 s** |
| LLM rounds | 23 |
| Fresh input tokens (full price) | **768,484** |
| Cache-read input tokens (~10% price) | **1,213,258** |
| Cache-creation tokens (~125% price) | 58,141 |
| Output tokens | 39,818 |
| **Recorded cost** | **$1.137** |

Cache served **59.5 %** of all input-side tokens (1.21 M of 2.04 M). That is the FRE-468/473 cache-control work doing its job — *without* it a 23-round, multi-breakpoint turn would have errored on the >4-breakpoint ceiling (the original 2026-06-04 incident) rather than run. But 40 % full-price re-billing is the headline inefficiency, and §4 explains why.

---

## 3. Methodology

**Data sources.** `agent-logs-*` (metadata + per-call cost/token fields, keyed on `event_type`) in `cloud-sim-elasticsearch` (`127.0.0.1:9200`). Per-turn captures live in `agent-captains-captures-*`. Gateway stdout carries only metadata and is interleaved with health-probe traces — *not* a reliable per-turn source.

**Timezone alignment (a recurring trap).** ES `@timestamp` is UTC. The owner's wall-clock is **UTC+2**, so the turn reported as "10:10 this morning" is `08:10` in ES. The container clock (`date -u` ≈ 11:18) and the ES timestamps must be reconciled before any "which turn" claim — getting this wrong cost a false start in the live session. (See the turn-forensics skill idea: timezone-aligned cross-substrate trace analysis.)

**Discipline.** Every claim below is backed by a query result, not inference. Every proposed lever is defined so it can be re-measured with the same per-round curve — before/after, not anecdote.

---

## 4. Finding 1 — the context tail is uncached and re-billed every round

The per-round progression is the whole story:

```
 #  fresh_in  cache_rd  cache_cr    out  lat_s   phase
 1     1,003         0         0     26    0.8   prelude
 2    14,803         0     8,886    173    3.1   ┐
 8    23,674    35,496       329    171    3.3   │ DISCOVERY
12    38,622    56,660     5,517    222    4.4   │ 17 serial tool rounds;
18    47,366    91,802       248    133    4.5   │ fresh_in climbs monotonically
19    50,778    92,092       330  3,189   70.9   ┐
20    54,019    92,532     1,165  2,132   43.9   │ GENERATION TAIL
21     2,596         0         0 16,384   96.1   │ sub-agent draft — OUTPUT-CAPPED
22    56,199    94,998     3,296 14,835  214.0   │ spill continuation (worst call)
23    71,214   107,934     2,184    594   16.3   ┘ assemble + commit
```
*(abridged; full 23 rows in the appendix query.)*

**Mechanics.** Anthropic prompt caching matches a prefix up to a `cache_control` breakpoint. FRE-468 clamps breakpoints to **≤4**, and they are placed on the *stable head* — system prompt, skills index, memory, early history. Tool results, however, are appended to the *tail* of the transcript verbatim, **past the last breakpoint**. So on every subsequent round the accumulated tool output is re-sent as **fresh, full-price input** — until enough of it ages into the cached prefix.

The signature: by round 22 the prompt is ~151 k tokens, of which ~95 k is cache-read but **56 k is fresh**, and that fresh chunk climbs every round (14 k → 71 k). Integrated over 23 rounds that is the entire **768 k** fresh-input bill. This is not the model "thinking hard" — it is the *same bash/read transcripts paid for five-to-ten times over*.

This is the **intra-turn** analogue of the cross-turn KV-reuse defect documented in `2026-06-02-cache-aware-prompt-layout-and-compaction.md`: the volatile, growing content sits where the cache cannot protect it.

---

## 5. Finding 2 — the generation tail dominates wall-time and hits the output cap

Discovery (17 rounds) is *cheap in time*: ~3–5 s per round. The cost in time is the **generation tail**:

- Rounds 19–22 latencies: 70.9 + 43.9 + 96.1 + 214.0 = **424.9 s = 48.6 %** of the 874 s turn, in **4 calls**.
- **Round 21 produced exactly 16,384 output tokens** — the `max_tokens` ceiling — then **spilled into round 22** (14,835 output, **214 s, the worst single call of the turn**).

So the artifact was generated in chunks **not by design but because it ran out of output budget**, adding a full round-trip (with the re-sent context tail from Finding 1) and risking a seam at the chunk boundary.

---

## 6. Latent constraint — `TOOL_USE` pins complexity to `SIMPLE`

FRE-469's minimal fix routes artifact intent to `TOOL_USE`, but in `request_gateway/intent.py` the `TOOL_USE` branch hard-sets `complexity = Complexity.SIMPLE`. The downstream decomposition assessor therefore returns `strategy=single` (`reason=tool_use_single`) — confirmed in the trace. **A turn that does 32 tool calls and builds a multi-section artifact is assessed "simple" and forced down the single-agent path.** The complexity pin actively *blocks* the decomposition that would relieve both findings above. This is the single highest-leverage architectural lever, and it is a side-effect of the minimal 469 fix — exactly the kind of thing a forensic pass exists to catch.

---

## 7. The levers, assessed and ranked

The owner proposed three; the data adds a fourth. Ranked by ROI-to-risk:

### ① Intra-turn tool-result compression — *highest ROI, lowest risk* → **FRE-475**
Direct hit on Finding 1. Compress large tool results to a compact digest **before they enter the transcript** (persist the full result to the object store, ADR-0069, for retrieval). Mid-phase tool outputs are ~3–6 k each; digesting to ~500 tokens flattens the fresh-input curve. Reuses FRE-434 (D2/D3) compaction machinery; the gap is doing it *intra-turn at insertion time*. Target: ≥30 % cut in total fresh input with no artifact-quality regression.

### ② Decomposition (HYBRID/DECOMPOSE) + unpin complexity — *highest ceiling* → **FRE-476**
Attacks **both** findings. Decomposed discovery: concurrent sub-agents each hold their own context slice and return a **digest**, so the parent tail never grows to 71 k and the 17 serial rounds parallelize. Sectioned generation: artifact parts drafted concurrently then assembled — parallelizes the 7-minute tail **and** sidesteps the 16 k output cap. Requires unpinning `TOOL_USE` complexity (§6) and a routing change; pairs with FRE-432 (ADR-0082 tier-aware routing). Highest reward, most design surface.

### ③ Discovery batching via compound bash — *cheap, behavioral* → **FRE-477**
The turn's first 9 bash calls were pure file-location (`find`/`ls`/`grep -l`); the rest were narrow `sed -n <range>` / `grep -n` slices — 20 bash invocations where one compound command (`find … | xargs grep -n …`, chained `&&`) returning a structured digest would collapse several. Each saved call removes a full round-trip *and* shrinks the tail. Prompt/governance lever, not architectural — lowest ceiling, dependent on model compliance, but trivially cheap to trial and complementary to ①.

### ④ Artifact output-cap fix — *quick independent win* → **FRE-478**
Raise/parameterize the artifact sub-agent `max_tokens` (via `settings`, not hardcoded) so a typical artifact fits one generation call, or handle continuation deterministically. Removes the round-21→22 spill (310 s combined) regardless of ①–③.

**Recommended sequence:** ① first (measurable, low-risk, reuses existing machinery) → ② (the ceiling-raiser, needs an ADR) → ③ in parallel as a cheap experiment → ④ as a standalone latency win.

---

## 8. What FRE-469 + the cache chain actually resolved

To keep the assessment honest about the *prior* wins this builds on:

| Failure family | Resolved by | Evidence in `a0a07227` |
|---|---|---|
| Capability-starvation by misroute (build → `conversational`, cap 6) | **FRE-469** | classifies `tool_use`, 23 rounds used, artifact committed |
| `cache_control` >4 breakpoints erroring long multi-breakpoint turns | **FRE-468** | 23-round turn ran without breakpoint error |
| Cross-turn cache decoration poisoning | **FRE-473** | 59.5 % cache-read ratio held across 23 rounds |

The turn that was *unbuildable this morning* now completes end-to-end at ~$1.14. This project is about making that same outcome **cheaper and faster**, not about correctness.

---

## 9. Reproducible measurement recipe

For any candidate fix, re-run an equivalent artifact prompt and compare against the `a0a07227` baseline with these queries (against the turn's `trace_id`):

1. **Event histogram** — `terms` agg on `event_type` (round count, tool mix, artifact lifecycle).
2. **Per-round token curve** — `model_call_completed` sorted ascending, project `input_tokens` / `cache_read_tokens` / `output_tokens` / `latency_ms`. Plot fresh_in vs round; a flatter curve = compression/decomposition working.
3. **Cost/cache aggregate** — `sum` over `api_cost_recorded` for `cache_read_input_tokens`, `cache_creation_input_tokens`, `cost_usd`.
4. **Wall-time-by-phase** — first/last `@timestamp`; isolate the generation tail latencies.

Report **before/after** tables, never single anecdotes (the `feedback_always_include_references` + side-by-side-eval discipline).

---

## 10. Open questions

- Where exactly should the intra-turn digest live — replace in-transcript, or keep a pointer the model can re-expand on demand?
- Does a 5th *rolling* breakpoint on the tail (within the ≤4→? envelope) help, or does it just thrash the cache? (Cross-reference ADR-0081 layout work.)
- For decomposition: what is the right complexity threshold so simple tool turns stay `single`?
- Interaction with FRE-432 tier-aware routing — do decomposed sub-agents inherit the non-thinking sub-agent tier?

---

## 11. References

- **Originating fix:** FRE-469 (classifier → `tool_use`; introduced the complexity pin) · PR #154
- **Cache chain:** FRE-468 (clamp `cache_control` ≤4), FRE-473 (copy-isolate cache decoration), FRE-434 / ADR-0081 §D2/D3 (frozen append-only layout + compaction scheduler)
- **Routing:** FRE-432 / ADR-0082 (tier-aware model selection)
- **Substrate:** ADR-0069 (R2 object store), ADR-0074 (identity threading / joinability)
- **Companion research:** `docs/research/2026-06-02-cache-aware-prompt-layout-and-compaction.md`
- **Tickets created from this doc:** FRE-475 (compression), FRE-476 (decomposition + complexity), FRE-477 (bash batching), FRE-478 (output cap) — project *Turn Cost & Latency Optimization (artifact builds)*
- **Post-mortem (origin incident):** `docs/postmortems/2026-06-04-artifact-turn-failure-cache-control.md`
