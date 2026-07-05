# FRE-802 — Live entity-extraction token cost + prompt-cache behaviour: decision (no change)

**Date:** 2026-07-05
**Backing:** [ADR-0109 Entity & Relationship Taxonomy Redesign](../architecture_decisions/ADR-0109-entity-taxonomy-redesign.md) (V2 10-type GoLLIE prompt, shipped FRE-771). Cost-hygiene sibling of [FRE-801](https://linear.app/frenchforest/issue/FRE-801) (same cache principle on the one-time migration classifier).
**Outcome:** **Already effectively cache-optimal — ship no live-prompt change.** The expensive static instruction block is already cached when the provider cache is warm; the only structural residual (a 340-token schema footer) is negligible and its placement is a deliberate quality choice. Recorded here per the ticket's measure-first path (a valid, complete outcome — no busywork change).
**Reproduce:** `uv run python scripts/research/fre802_extraction_cache_probe.py`

---

## The question

The live entity extractor (`second_brain/entity_extraction.py`) makes one LLM call per
conversation turn, re-sending the full V2 GoLLIE definition block every time. The ticket asks a
measure-first question: **is that big static block already being billed at the discounted "cached"
rate, or are we paying full price to re-send it on every turn?** If it is already cached, do
nothing; if it is not, restructure the prompt so the static part is a cacheable prefix.

**Plain-language primer on "prompt caching."** When you send the same opening text to an LLM
provider repeatedly, the provider can remember the tokens it already processed and charge a
fraction of the price for that repeated *prefix* — like a shop letting you skip re-scanning items
you bought yesterday. Two rules matter here: (a) the repeated part must be at the **very start** of
the prompt — the discount stops at the first byte that differs between calls (the "variable" turn
text); and (b) for OpenAI the prompt must be **≥ ~1024 tokens** and the cache is **short-lived** —
the memory of your prefix fades after a few minutes of not seeing it. ("Token" ≈ ¾ of a word;
"prefix" = the leading run of text that is byte-identical call to call.)

The live model for this role is **`gpt-5.4-mini` on OpenAI** (`config/model_roles.yaml`:
`entity_extraction → gpt-5.4-mini`; input priced $0.75/MTok, output $4.50/MTok). It is the *only*
role mapped to that model — captains_log/insights use claude_sonnet, primary uses claude — so a
`model_call_completed` event tagged `openai/gpt-5.4-mini` is, by construction, an extraction call.

## What the prompt actually looks like (source measurement)

`scripts/research/fre802_extraction_cache_probe.py` measures the live template constants directly
(tiktoken `o200k_base`, the gpt-5-series encoder):

| Component | Tokens | Cacheable? |
|---|---:|---|
| System prompt | 95 | ✅ prefix |
| Static instruction head — entity/relationship types, class rules, examples (few-shot OFF) | 3 084 | ✅ prefix |
| Few-shot exemplar block (flag-gated; **OFF** in config) | 398 | ✅ prefix (when on) |
| **Cacheable static prefix (few-shot OFF)** | **3 179** | ✅ well above the ~1024 floor |
| Variable turn content (`User:` / `Assistant:` text) | ~300–900 | ❌ always full price (this is the payload) |
| Static JSON-schema footer (`Return ONLY valid JSON: {…}`) | 340 | ❌ **sits after the variable turn** |

The prompt assembly order (`_EXTRACTION_PROMPT_TEMPLATE`, `entity_extraction.py:62-281`) is:

```
[system prompt]  [instructions + types + rules + examples]  [few-shot?]     ← 3 179 tok, byte-identical every call
Conversation: User: {user_message}  Assistant: {assistant_response}         ← VARIABLE (the cacheable prefix ends here)
Return ONLY valid JSON: { …340-token output schema… }                       ← static, but positioned AFTER the variable turn
```

**Finding 1 (structure).** The big, expensive instruction block (3 179 tokens — ~80% of a typical
input) is a clean cacheable prefix: it precedes the first variable byte, so nothing truncates it.
The one imperfection is the 340-token JSON-schema footer, which is static but positioned *after* the
variable turn text, so it can never join the cached prefix and is re-billed at full price every
call.

## What the provider actually does (live telemetry)

Aggregating `model_call_completed` for `openai/gpt-5.4-mini` in the current prompt regime
(since the V2 cutover on 2026-07-02; N = 27 calls):

| Metric | Value |
|---|---|
| Input tokens/call | min 2 805 · median 3 518 · mean 3 449 · max 4 082 |
| **Cache hit rate** (calls with `cache_read_tokens > 0`) | **12 / 27 (44%)** |
| Cached tokens when warm | 1 792 or 2 816 (OpenAI caches in 128-token blocks) |
| Cached share of input when warm | median 0.52 · max 0.75 |
| Output tokens/call | median 1 022 |
| Cost/call | mean **$0.00674** · 4-day window total **$0.18** |

**Finding 2 (the cache works when warm).** When two extractions run close together, OpenAI charges
the discounted rate on ~half to three-quarters of the input — i.e. the 3 179-token instruction block
*is* being cached. The measured live input size (median 3 518, matching the source prefix 3 179 +
turn) also confirms **no source-vs-deploy drift**: prod is running exactly this V2 prompt.

**Finding 3 (the real limiter is time, not structure).** Only 44% of calls hit the cache; the other
56% run **cold** (`cache_read_tokens = 0`) despite an identical prefix. Cause: extraction is
temporally sparse — it fires during consolidation, and when two turns are more than a few minutes
apart, OpenAI has already evicted the prefix from its short-lived cache, so the next call re-pays in
full. This is exactly the signal the ticket flagged from the FRE-772 migration ("cached=0 on the
first pass, cached only on warm re-runs"). It is a *cadence* property of the workload, **not** a
defect in prompt layout — reordering bytes cannot make a cold cache warm.

**Finding 4 (the regime change is the real cost mover).** A daily breakdown shows extraction input
size roughly **doubled on 2026-07-02** — from ~1 600 tokens/call to ~3 400 — exactly when the V2
10-type GoLLIE prompt shipped (FRE-771). That is the dominant driver of extraction input cost, and
it is a deliberate quality investment (the richer taxonomy), not waste. Cache hits only began
appearing in this same window, because the pre-V2 prompt spent much of its life below effective
caching benefit.

## Decision — no change

Restructuring the prompt to move the 340-token footer ahead of the conversation is **not worth it**:

1. **Negligible upside.** The footer is 340 tokens. Moving it into the cacheable prefix helps *only*
   on the 44% of calls that hit the cache at all (on a cold call nothing is cached and OpenAI does
   not bill for cache writes, so byte order is irrelevant). Even assuming every call were warm, the
   saving is ~340 tok × $0.75/MTok × the cache discount ≈ **$0.02/month** at current volume
   (~$1.4/month total extraction spend). Below measurement noise.

2. **Real regression risk on a sensitive surface.** The output-schema footer sits *last* on purpose:
   models follow the final instruction before generation most strongly, so "return exactly this JSON
   shape" as the closing line is a deliberate format-adherence lever. Moving it earlier trades a
   sub-cent cache gain for a real risk of malformed-JSON extraction. And the live extraction prompt
   is a master-gated, coordinated-deploy surface (the one FRE-771 swapped) — changing it would demand
   a full FRE-771-style no-regression A/B (100+ LLM calls). High cost, negative expected value.

3. **The expensive part is already cached.** The 3 179-token instruction block — the actual cost — is
   already a valid cacheable prefix and is discounted whenever the cache is warm. There is no
   structural cache truncation to fix on the part that matters.

**One-paragraph "why not" (ticket AC):** The live extractor already lays its 3 179-token instruction
block as a clean cacheable prefix, and OpenAI discounts it on the 44% of calls that land while the
prefix is still warm — the block *is* being cached. The only imperfection is a 340-token JSON-schema
footer positioned after the variable turn; moving it would save roughly two cents a month, only on
already-warm calls, while risking JSON-format regressions on a master-gated deploy surface. The
dominant cost mover is the deliberate V2 taxonomy (which doubled input size for quality) and the
inherent sparsity of extraction calls (which leaves 56% cold no matter how the bytes are ordered) —
neither is fixable by prompt restructuring. Nothing ships.

## Secondary assessment — batching turns (assessed, not implemented)

The static prefix is ~80% of each call's input and is re-sent per turn, so the one lever with real
leverage is **batching N turns into one extraction call**, amortizing the 3 179-token block across
N turns (an ~N× cut in per-turn static cost). It is **not worth doing now**:

- **It breaks per-turn provenance.** `trace_id`, `attempt_number`, `turn_timestamp`, and `observed_at`
  are per-turn by contract (ADR-0074 identity threading, ADR-0098 bitemporal supersession). One call
  spanning many turns cannot carry distinct per-turn identity/observation times without a substantial
  redesign of the consolidation write path.
- **The absolute spend does not justify it.** Total extraction cost is ~$1.4/month. The provenance
  complexity vastly outweighs the saving at this volume.

**Sensitivity / when to revisit:** if extraction volume grows ~10–50× (heavy sustained chat), the
per-call static re-send becomes the dominant cost and batching's leverage would then justify the
provenance work. A cheaper intermediate lever exists first — have consolidation drain its backlog in
one tight burst (per-turn calls run back-to-back) so each call warms OpenAI's cache for the next,
lifting the 44% hit rate without touching provenance or the prompt. Both are deferred, not filed, per
the no-busywork rule; this note is the pointer if volume ever moves.

## What this does not change

- The live extraction prompt, `taxonomy.py`, and the ADR-0109 10-type drift-guard are untouched.
- No follow-up ticket is filed — the measured verdict is "already optimal," and filing "maybe batch
  later" work would contradict it (the sensitivity trigger above is the durable pointer instead).

## References

- Live surface (unchanged): `src/personal_agent/second_brain/entity_extraction.py`
  (prompt `:62-281`; cloud dispatch + `cache_read_tokens` capture in
  `src/personal_agent/llm_client/litellm_client.py:559-707`).
- Role mapping: `config/model_roles.yaml` (`entity_extraction → gpt-5.4-mini`); pricing
  `config/models.cloud.yaml` / `config/models.yaml` (`gpt-5.4-mini`).
- Reproducible probe: `scripts/research/fre802_extraction_cache_probe.py` (token components +
  live ES cache aggregation).
- Telemetry: `agent-logs-*` `model_call_completed` events (`event_type`, `model`, `input_tokens`,
  `cache_read_tokens`, `cost_usd`); OpenAI `cached_tokens` capture per
  `llm_client/litellm_client.py:575-590`.
- Sibling: [FRE-801](https://linear.app/frenchforest/issue/FRE-801) — same cache-stable-prefix
  principle applied to the one-time FRE-772 migration classifier.
- Discipline model: [FRE-797 no-change note](2026-07-05-fre-797-phenomenon-domain-sharpening.md).
