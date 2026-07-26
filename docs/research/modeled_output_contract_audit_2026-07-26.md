# Modeled data requested as free-form prose — inventory and recommendation

**Ticket:** FRE-995 · **Date:** 2026-07-26 · **Author:** build session (Opus)
**Related:** FRE-993 (digest output ceiling) · FRE-994 (compression curve) · FRE-996 (digest JSON-contract pilot)
**Status:** inventory only. No call site is changed by this work — that is FRE-996's job, deliberately scoped to one subject.

---

## 1. What this audit does and does not claim

The ticket asked for an inventory of every model call that produces data intended to fit a
defined shape, recording five fields per call, measuring failure rates where the telemetry
allows it, and then grouping the calls by whether a structured-output contract is appropriate.

That is what follows. Three of the ticket's own premises did not survive contact with the
source, and they are corrected in §6 rather than buried — one of them materially changes what
FRE-996 should expect to measure.

**Window for every measurement:** 2026-07-12T00:00:00Z → 2026-07-26T23:59:59Z (14 days).
**Sources:** Postgres `api_costs` (cloud-sim-postgres), Elasticsearch `agent-logs-*`
(cloud-sim-elasticsearch :9200). Every number below carries the query or table that produced it.

---

## 2. Method — why this enumeration is complete

`LLMClient` exposes exactly two generation entrypoints (`llm_client/client.py`):

- `async def respond(...)` — line 148
- `def get_dspy_lm(role)` — line 665

`LiteLLMClient` exposes one, `respond()` (line 313). So the complete set of generation call
sites is `grep '\.respond('` plus the DSPy consumers, and there is no third path to miss.

Two direct-provider calls exist and are **not** generative: `memory/embeddings.py:548`
(`openai.AsyncOpenAI`, embeddings) and `memory/reranker.py:214` (raw `client.post`, Voyage
reranker). Neither produces modeled text, so neither is in scope.

Three `json.loads` sites that look like model-output parsing are not:

| Site | What it actually parses |
|---|---|
| `orchestrator/tool_result_digest.py:139, 435` | Stored **tool-result payloads** in the deterministic extractors (D2). No model call in the module. |
| `orchestrator/context_compressor.py:355` | `_shape_descriptor` on a **tool payload**, to build a one-line descriptor. |
| `orchestrator/executor.py:4944` | **Tool result content**, to inject a gate advisory. |

That leaves **12 generation call sites**, inventoried below.

---

## 3. Inventory

Columns are the five the ticket asked for. "Schema shown to the model" distinguishes a shape
*described in the prompt* from a shape *enforced by the provider* — that distinction is the
whole finding, so it is split into two columns.

### Group A — modeled output requested in prose (the pattern)

| # | Call site | What it asks for, how | Receiving-side schema | Shape in prompt | Provider-enforced | Failure detection | Observable? |
|---|---|---|---|---|---|---|---|
| A1 | `second_brain/session_summary.py:448` (cloud) / `:464` (local) — **session digest** | `label` + `SessionDigest` as JSON, asked in English. System prompt embeds a literal JSON shape (lines 112–137). No `tools=`, no `response_format=`. | Yes — `DigestItem`, `UnresolvedItem`, `Correction`, `SessionDigest` (Pydantic, frozen) in `memory/session_digest.py`. Used only to *construct after* hand-validation; `SessionDigest.model_validate` is never called on the reply. | Yes (prose) | **No** | `parse_model_output()` (line 370): `orjson.loads(_strip_fences(...))` → hand-checks every field → `ValueError`. One retry (`_MAX_GENERATION_ATTEMPTS = 2`). | **Yes, well.** Every path goes through `_failed()` (line 482) → `session_summary_failed` with a `failure_reason` from a closed enum. Best-instrumented site in the tree. |
| A2 | `second_brain/entity_extraction.py:761` (cloud) / `:809` (local) — **entity extraction → Neo4j** | Entities / relationships / stances / claims as JSON. `_EXTRACTION_SYSTEM_PROMPT` says "valid JSON only — no markdown fences". `_EXTRACTION_PROMPT_TEMPLATE` enumerates 10 entity types, 6 relationship types, 3 output kinds in ~200 lines of prose. No `tools=` (explicitly `tools=None`), no `response_format=`. | **No — none exists.** The reply is raw `dict` from `result.get("entities", [])` all the way into `_finalize_extraction` and on into Neo4j. No Pydantic model, no TypedDict for the payload. | Yes (prose, extensive) | **No** | Fence-strip + outermost-`{` recovery, then `orjson.loads` → `entity_extraction_json_parse_failed` (ERROR, with a `likely_truncated` flag) → `_default_extraction_result` (empty). | **Split.** Parse failure: yes, loudly. **Field-level failure: no.** See §5.1 — off-vocabulary enum values are silently coerced to a default with no log line, and `entity_type` is validated nowhere at all. |
| A3 | `captains_log/reflection.py:423` — **reflection (manual path)** | Reflection JSON. `temperature=0.3` "for structured output". No `tools=`, no `response_format=`. | Partial — `CaptainLogEntry` exists, but `_parse_reflection_response` returns a bare `dict[str, Any]`. | Yes (prose) | **No** | Fence-strip by `split("```json")` → `json.loads` → `ValueError` → `_create_basic_reflection_entry` fallback. | Yes — `reflection_parse_failed` (WARNING). |
| A4 | `orchestrator/skills.py:451` — **skill routing** | A JSON array of skill names. `max_tokens=256`, `temperature=0.0`. No `tools=`, no `response_format=`. | Weak — `isinstance(parsed, list)` plus a membership filter against `cache.docs`. No model. | Yes (prose) | **No** | `raw.strip("`").lstrip("json")` fence-strip → `json.loads` → `skill_routing_parse_failed` → `return []`. | Yes — but the *consequence* is invisible: the fallback `[]` is indistinguishable from a legitimate "no skills apply". |
| A5 | `orchestrator/expansion_controller.py:277` — **expansion planner** | An `ExpansionPlan` as JSON. `max_tokens=1024`. **The only site in the tree that passes any structured-output directive:** `response_format={"type": "json_object"}` (line 281). | Yes — `ExpansionPlan` / `PlanTask`, but `_validate_plan_json` (line 520) hand-walks the dict and returns `None` on any problem. | Yes (prose) | **Partial** — JSON mode guarantees syntactic validity, *not* conformance to `ExpansionPlan`. | `_validate_plan_json` returns `None`; caller treats it as planning failure. | **Weakest of the group.** The `None` return has no dedicated failure event — a rejected plan is not separately countable. |

### Group B — declared schema, given to the model, not provider-enforced

| # | Call site | Notes |
|---|---|---|
| B1 | `captains_log/reflection_dspy.py` (via `get_dspy_lm`) | A real `dspy.Signature` (`GenerateReflection`, line 77) with 11 typed `InputField`s and 9 `OutputField`s, each carrying a `desc`. DSPy renders this into the prompt and parses the reply against it — so the shape *is* declared and machine-associated, just not provider-enforced. **Caveat:** every `OutputField` is flattened to `str`, so enums are re-parsed by hand afterwards (`_parse_enum` line 199, `parse_missing_skill_names` line 217). This is the closest thing to a contract in the codebase and it still hand-parses its enums. |

### Group C — already enforced by a tool schema

| # | Call site | Notes |
|---|---|---|
| C1 | `orchestrator/executor.py:4266` — the primary turn | Passes `tools=tools if tools else None` (line 4270). Tool arguments arrive as native `tool_calls` and are parsed at `:4783`. This is the one place in the system where model output is genuinely schema-constrained. **Degradation path worth noting:** `llm_client/tool_call_parser.py` exists to recover tool calls from *free text* when a provider does not return native `tool_calls` — i.e. the fallback for this site is the very pattern this audit is about. Also note the inverse repair at `executor.py:1718` (`_unwrap_embedded_response_json`), which unwraps a model that emitted router-style JSON when prose was wanted. |

### Group D — prose by design (correctly prose)

| # | Call site | Output | Verdict |
|---|---|---|---|
| D1 | `orchestrator/context_compressor.py:246` | A ≤200-word prose summary (`max_tokens=320`). Empty reply → `FALLBACK_MARKER` + `context_compression_empty_response`. | Genuinely prose. Forcing a schema here would be the same mistake inverted. |
| D2 | `orchestrator/sub_agent.py:230` | A sub-agent answer. `spec.output_format` is a free-text hint in the user turn ("Output format: …"). | Genuinely prose — the format varies per task by design. |
| D3 | `captains_log/feedback.py:137` | A Linear comment body, returned as text. Failure → a prose apology string. | Genuinely prose. |
| D4 | `memory/service.py:215` | Multiquery paraphrases, `max_tokens=200`, split on newlines, `lines[:count]`. Fails open to `[]` per ADR-0104/FRE-723. | Weakly structured (line-per-item), not JSON. A contract is possible but low-value — see §8. |

### Group E — markup output

| # | Call site | Notes |
|---|---|---|
| E1 | `tools/artifact_tools.py:1601` — artifact builder | Produces an **HTML document**, not JSON. Post-hoc `_strip_code_fences(html_content)` — the same fragile fence-unwrap as Group A, applied to markup. JSON structured outputs are the wrong tool here; the fence handling is still worth tightening. |

---

## 4. Measured failure rates

### 4.1 Billed calls, output ceilings and spend — `api_costs`, 14 days

```sql
SELECT purpose, count(*) calls, min(output_tokens), 
       percentile_disc(0.5) WITHIN GROUP (ORDER BY output_tokens) p50,
       max(output_tokens), count(*) FILTER (WHERE output_tokens = 2048) eq2048,
       round(sum(cost_usd),4) usd
FROM api_costs WHERE timestamp >= '2026-07-12' GROUP BY purpose;
```

| `purpose` | Site | Calls | p50 out | max out | Ceiling | At ceiling | Spend |
|---|---|---:|---:|---:|---:|---:|---:|
| `captains_log` | A1 digest | 446 | **2048** | **2048** | 2048 | **254 (56.9%)** | **$14.19** |
| `main_inference` | C1 + D1 + A5 (shared role) | 112 | 760 | 7872 | varies | 0 | $8.61 |
| `skill_routing` | A4 | 80 | 13 | **139** | 256 | **0** | $0.11 |
| `entity_extraction` | A2 | 72 | 1020 | **5416** | 10000 | **0** | $0.51 |
| `artifact_builder` | E1 | 10 | 13178 | 23551 | high | 9 ≥10k | $1.65 |
| `embedding` | — | 80 | 0 | 0 | — | — | $0.0002 |

The digest's 446 calls / 57%-at-ceiling / $14.19 figures in the ticket **verify exactly**.

### 4.2 Failure events — `agent-logs-*`, 14 days

Terms aggregation on `event_type`, then on `failure_reason`. Note the field-loss caveat below.

| Event | Count | Denominator | Rate |
|---|---:|---|---|
| `session_summary_failed` | **1207** | 446 billed calls (retries inflate this) | see taxonomy |
| `skill_routing_parse_failed` | **13** | 80 `skill_routing_call_completed` | **16.3%** |
| `entity_extraction_json_parse_failed` | **0** | 73 `entity_extraction_completed` | **0.0%** |
| `entity_extraction_failed` (all causes) | 3 | 73 | 4.1% |
| `extraction_empty_response` | 0 | 73 | 0% |
| `reflection_parse_failed` | 0 | — | not exercised (reflection is disabled) |
| `context_compression_empty_response` | 0 | — | 0% |

**Digest failure taxonomy** (1207 total; 682 recovered from `_source` — see §5.2):

| `failure_reason` | Count | Mechanism |
|---|---:|---|
| `budget_denied` | 1078 | Downstream of the retry loop (FRE-987), not a parse failure |
| `empty_output` | 114 | `"model returned nothing"` — content empty |
| `schema_invalid` | 12 | **Every sampled `detail` is `"output is not valid JSON: unexpected end of data"`** — i.e. truncation, not format drift |
| `span_validation_failed` | 2 | Provenance check |
| `digest_over_budget` | 1 | Digest exceeded the token bound |

### 4.3 The regime change — the pattern was latent for months

```sql
SELECT date_trunc('day',timestamp)::date, count(*), count(*) FILTER (WHERE output_tokens=2048)
FROM api_costs WHERE purpose='captains_log' AND timestamp >= '2026-07-12' GROUP BY 1;
```

| Date | Calls | At 2048 ceiling |
|---|---:|---:|
| 07-13 → 07-22 | 58 | **0** |
| 07-23 | 142 | 14 |
| 07-24 | 53 | 49 |
| 07-25 | 111 | **111 (100%)** |
| 07-26 | 82 | 80 (98%) |

Zero of the first 58 calls hit the ceiling. From 07-24 onward it is effectively total. ADR-0124
Amendment B (`82f71be3`, `feat(FRE-956): narrow session-digest producer to conversation-only`,
merged 2026-07-24) removed the 200-char excerpt clip and the 20-turn cap, expanding input by
roughly an order of magnitude on the assistant side. The digest's output grew with it and hit a
ceiling that had never been reached before.

**This is the causal chain that matters:** the prose-JSON pattern sat harmless for months, and an
unrelated input-scope change converted it into a ~100% failure rate. The pattern is a latent
fault, not an active one — which is exactly why an inventory is the right first deliverable.

---

## 5. Observability gaps found while measuring

### 5.1 Entity extraction coerces off-vocabulary model output silently

The extraction prompt constrains the model with prose enumerations, and Python then defaults
anything off-vocabulary **without emitting a log line**:

| Helper | Behaviour | Comment in source |
|---|---|---|
| `_valid_entity_class` (`entity_extraction.py:478`) | Off-vocabulary → `World` | *"fail-open, ADR-0115 D4"* |
| `_valid_output_kind` (`:498`) | Off-vocabulary → `knowledge` | *"fail-open, ADR-0115 D4"* |
| `_normalize_update_kind` (`:394`) | Off-vocabulary → `new` | FRE-712 |
| `_valid_description_signal` (`:470`) | Off-vocabulary → `new` | FRE-725 |

`output_kind` is the **routing axis** for ADR-0115 (knowledge / ephemeral / finding). A model that
guesses it wrong has its output routed as `knowledge` regardless, and nothing records that the
guess was wrong. Measured failure rate for these: **unmeasurable, by construction** — there is no
event to count.

Worse: `entity_type` is validated **nowhere**. `grep` for `VALID_ENTITY_TYPES` / `entity_type not in`
across `src/` returns nothing, and `memory/service.py:1080` / `:1947` MERGE every entity as
`(e:Entity {name: $name})` with the model's `type` string as an ordinary property. The 10-type
vocabulary of ADR-0109 V2 exists only in the prompt.

This is the audit's most consequential finding after the digest itself: **entity extraction's
0/73 parse-failure rate is not evidence that the path is healthy.** It is evidence that the
*syntactic* failure mode does not fire there. The semantic failure mode is unmeasured because it
is unmeasurable.

### 5.2 The digest's failure taxonomy was silently dropped by ES for one day

`failure_reason` is not indexed on `agent-logs-2026.07.23` (682 of 1207 failures), so a terms
aggregation attributes only 525. The field **is** present in `_source` and was recovered by
scrolling the index in Python — hence the complete taxonomy in §4.2 — but it is not queryable,
so any Kibana panel or dashboard keyed on `failure_reason` under-reports that day by 56%. This is
the ES field-limit class of problem FRE-983 addressed (300 → 1000); worth confirming the fix is
live on the deployed template.

---

## 6. Corrections to the ticket's premises

Stated plainly because one of them changes FRE-996's hypothesis.

### 6.1 "Roughly ten modules" over-counts. The true figure is five.

The ticket lists the session summary producer, entity extraction, Captain's Log reflection, the
Linear feedback path, the context compressor, the skills module and the orchestrator executor as
sharing the hand-parse pattern. Of those:

- **`captains_log/feedback.py` does not parse anything.** `_feedback_llm_complete` returns
  `(resp.get("content") or "").strip()` directly — the output is a Linear comment body. Prose by design.
- **`orchestrator/context_compressor.py` does not parse model output.** It asks for a ≤200-word
  prose summary and reads `response.get("content")` as a string. Its `json.loads` at line 355 is
  `_shape_descriptor`, which inspects **tool payloads**.
- **`orchestrator/executor.py` is the one site that already passes tool schemas** (line 4270). Its
  `json.loads` calls parse tool *results* and a router-JSON repair, not a modeled reply.

Five sites genuinely request modeled data in prose: A1–A5. That is a materially smaller and more
tractable surface than the ticket assumed, and it means the recommendation in §8 is a short list.

### 6.2 "The model is never shown the shape it must produce" — it is, in prose

`session_summary.py:112–137` embeds a literal JSON shape in the system prompt, including the
`item` and `correction` sub-shapes and the three legal `basis` values. `_system_prompt()`'s own
docstring says so: *"the prompt embeds a literal JSON schema, and every brace in it would have to
be doubled to survive `str.format`."* Entity extraction goes further, with a ~200-line prose
specification of every enum.

What is absent is **provider-side enforcement**, not description. This matters for FRE-996's
framing: the pilot's intervention is not "show the model the shape" (already done, thoroughly) but
"move the shape from prose the model may ignore to a constraint the decoder cannot violate."
A pilot framed as the former would measure a change that has already been made.

### 6.3 "Giving the model the schema converts unparseable output from a failure mode into an impossibility" — overstated

**A structured-output contract does not prevent truncation.** Anthropic's own structured-outputs
documentation states it directly: *"If `stop_reason: "max_tokens"`, output may be incomplete.
Increase `max_tokens`."* A schema-constrained generation cut off at the output ceiling still
yields a JSON fragment. The same holds for forced tool use: a truncated `tool_use` block carries
partial input JSON.

Every sampled `schema_invalid` detail is `"unexpected end of data"` — truncation, 100% of the
sample. So **a contract alone would not have prevented the digest incident.** The sizing work
(FRE-993 ceiling mismatch, FRE-994 compression curve) is load-bearing, not complementary polish.

What a contract *does* buy, precisely:

| Eliminated | Not eliminated |
|---|---|
| Fence wrapping and trailing prose (the `_strip_fences` / `strip("`")` heuristics) | Truncation at `max_tokens` |
| Key-name drift and missing required keys | Refusals (`stop_reason: "refusal"` bypasses the schema) |
| Enum drift (`basis`, `tier`, `output_kind`, `entity_type`) | Semantic wrongness within a valid shape |
| A silent parse exception, replaced by a loud `stop_reason` | Constraints the schema dialect cannot express (§7) |

---

## 7. The two failure modes are distinct, and each has one correct fix

The measurements separate cleanly, and conflating them would mis-target the remediation.

**Mode 1 — truncation.** The digest. Ceiling (2048) below required output; p50 = max = 2048;
100% of parse failures are `unexpected end of data`. **A contract does not fix this. Sizing does.**

**Mode 2 — format drift.** Skill routing. `max_tokens=256`, observed max output **139 tokens** —
never remotely near the ceiling — yet **13/80 = 16.3%** fail to parse. The `raw_preview` field
shows the mechanism exactly:

```
'[]\n```\n\nThe user is asking "Image tests. What do you see?" but has not provided any image...'
```

The model complied with the JSON *and then appended an explanation after the closing fence*. The
unwrap at `skills.py:493` (`raw.strip("`").lstrip("json").strip()`) handles a fence-wrapped payload
with nothing after it, and fails on trailing prose. **A contract fixes this completely. A bigger
ceiling does nothing for it.**

**Calibrating mode 2's impact honestly:** 12 of the 13 failures had `[]` as the model's actual
answer, and the fallback is `return []` — so the fallback value coincided with the intended answer.
The 13th returned `["artifact_read"]`, a skill name not in `cache.docs`, which the membership
filter would have dropped anyway. **Measured user-visible harm from skill routing's 16.3% failure
rate over 14 days: zero.** The mechanism is broken; the blast radius here is not. Reporting it as
a 16% failure rate without that qualifier would overstate it.

---

## 8. Recommendation

### 8.1 Capability is already wired — verified live, no plumbing work

| Check | Result |
|---|---|
| `LiteLLMClient.respond` accepts + forwards `response_format` | Yes — `litellm_client.py:321`, forwarded at `:412` |
| `LiteLLMClient.respond` accepts + forwards `tools` | Yes |
| Local SLM path forwards `response_format` to llama-server | Yes — `adapters.py:537` → payload at `:628` |
| litellm version installed | **1.89.2** (pin `>=1.84.0`) |
| `litellm.supports_response_schema('anthropic/claude-sonnet-5')` | **True** |
| `litellm.supports_response_schema('openai/gpt-5.4-mini')` | **True** |
| `response_format` in supported params for all three deployed models | **True** (sonnet-5, gpt-5.4-mini, haiku-4-5) |

Every Group A site can pass a schema **today**, with no client change. The gap is entirely at the
call sites.

### 8.2 Constraints a contract cannot carry

From Anthropic's structured-outputs schema limitations, checked against our models:

| Constraint | Effect on us |
|---|---|
| No `maxLength` / `minLength` | **`MAX_LABEL_CHARS = 90` must stay a Python check.** A contract cannot enforce the label bound. |
| No numeric constraints (`minimum`/`maximum`/`multipleOf`) | `_coerce_mastery`'s clamp stays in Python. |
| No recursive schemas | None of ours recurse. Clear. |
| `additionalProperties: false` required on every object | Fine — Pydantic emits this. |
| `Literal` → `enum` supported; `$ref`/`$defs` supported | `BasisTag` and `CorrectionTier` are `Literal` (`session_digest.py:58, 68`) → enforceable. Nested `Locator` / inherited `Correction` → `$defs`, supported. |
| First request per schema pays a compilation cost; 24h schema cache | A per-call-site latency note, not a blocker. Relevant for a low-volume path like the digest, whose schema may recompile between sweeps. |

**The single most important implementation note for FRE-996:** `SessionDigest.model_json_schema()`
**cannot be used as the wire contract as-is.** `UnresolvedItem.as_of` is a producer-stamped field
— ADR-0124 D3, "computed state is never regenerated in prose", and `parse_model_output` stamps it
from `ended_at` at line 418. Handing the storage model's schema to the provider would *ask the
model for `as_of`*, directly violating D3. FRE-996 needs a **wire model distinct from the storage
model**, containing only model-authored fields. This is a design decision, not a mechanical
translation, and it is the reason a pilot is the right shape for this work.

### 8.3 Sites that should adopt a contract

| Site | Why | Which mechanism | Priority |
|---|---|---|---|
| **A1 digest** | The measured subject. Eliminates fence/enum drift; makes the residual failure a loud `stop_reason` rather than a parse exception. **Must ship alongside the sizing fix, not instead of it** (§6.3). | `response_format` json_schema over a **new wire model** (§8.2) | FRE-996 — pilot, already scoped |
| **A4 skill routing** | The cleanest case in the tree: 16.3% measured failure, zero truncation, mechanism identified exactly. A trivial schema (`{"skills": ["..."]}` with an enum of `cache.docs` keys) makes it unfailable, and an enum would additionally have caught the hallucinated `artifact_read`. Cheapest possible confirmation of the hypothesis on a second subject. | `response_format` json_schema | High — the natural second pilot |
| **A5 expansion planner** | Already passes the weak form. Upgrading `{"type": "json_object"}` → a json_schema over `ExpansionPlan` is a one-line strengthening of an existing directive, and would let `_validate_plan_json` shrink to a `model_validate`. Also add a failure event — currently the only Group A site whose rejections are uncountable. | Upgrade existing `response_format` | Medium |
| **A2 entity extraction** | Shares the pattern and writes to the KG. **But its urgency is semantic, not syntactic** — 0/73 parse failures, 5416 max output against a 10000 ceiling, so it is not truncating and will not benefit the way the digest does. The real win is enum enforcement: `entity_type`, `output_kind`, and `class` become undrifted at the decoder instead of silently coerced (§5.1). Needs a receiving-side model first — it currently has none. | json_schema **plus** a new Pydantic payload model | Medium — sequence after the pilot; the schema-design work is the bulk of it |
| **A3 reflection** | Would benefit, but reflection is currently disabled (`reflection_parse_failed` = 0 over 14 days, and the recall path was disabled 2026-07-26). **Do not touch until it is re-enabled and re-measured** — changing a disabled path buys nothing and adds risk. Note it also has two competing paths (manual JSON vs. the DSPy signature, B1); pick one before adding a third contract. | Deferred | Low |

### 8.4 Sites that should stay as they are

| Site | Why forcing a schema would be wrong |
|---|---|
| **D1 context compressor** | Output is a prose summary. Its value *is* the prose. A JSON envelope would add tokens to a path deliberately capped at 320. |
| **D2 sub-agent** | Output format varies per task by design (`spec.output_format` is a per-call hint). Any fixed schema would be wrong for most invocations. |
| **D3 Linear feedback** | Output is a human-readable comment. |
| **D4 multiquery paraphrase** | Line-delimited output, 200-token cap, fails open to `[]` by ADR-0104 design. A `{"paraphrases": [...]}` contract is *possible* and would be marginally more robust than newline-splitting, but the failure mode (fail open, dense arm continues) is already benign and measured at zero. Not worth the change on its own; fold it in only if someone is already in the file. |
| **E1 artifact builder** | Produces HTML. JSON structured outputs do not apply. **Separate, real issue:** `_strip_code_fences` on markup is the same fragile unwrap as Group A — if the model appends commentary after the fence, the artifact carries it. Worth a targeted fix, unrelated to structured outputs. |
| **C1 primary turn** | Already schema-constrained via `tools=`. No change. Its text-parsing fallback (`tool_call_parser.py`) is a genuine instance of the pattern, but it exists precisely to handle providers that don't support the native form — removing it would reduce robustness, not increase it. |
| **B1 DSPy reflection** | Has a declared signature. The gap is that all `OutputField`s are `str`, forcing hand enum-parsing. If reflection is revived, typed DSPy output fields are the cheaper fix than a parallel `response_format` path. |

### 8.5 Follow-up work this audit surfaced

Not filed as tickets — flagged for master and the owner to sequence, to avoid a ticket explosion
around a single finding:

1. **Entity-extraction fail-open coercions are unobservable** (§5.1). Four helpers silently default
   off-vocabulary model output; `entity_type` is unvalidated end to end. This is a genuine,
   separately-sequenceable observability gap and the strongest candidate for its own ticket —
   an emitted counter per coercion would turn "unmeasurable" into "measured" and would tell us
   whether A2's contract adoption is urgent or merely tidy. **Recommend filing.**
2. **`failure_reason` unindexed on 2026-07-23** (§5.2). Confirm FRE-983's 300 → 1000 field-limit fix
   is live on the deployed index template; any dashboard keyed on that field under-reports.
3. **Expansion planner has no failure event** (A5). One log line; fold into whatever touches that
   file next.
4. **`artifact_tools._strip_code_fences` on HTML** (E1). Same fragile unwrap, applied to markup.
5. **Reflection has two competing output paths** (A3/B1) and neither is currently exercised.
   Resolve which one lives before adding a contract to either.

---

## 9. Proof of the acceptance criteria

| Criterion (from FRE-995) | Where satisfied |
|---|---|
| Every model call producing modeled data appears with the five fields, sourced from code | §3, all 12 sites, every claim carrying a `file:line` |
| Enumeration is complete, not sampled | §2 — two generation entrypoints only; three look-alike parse sites excluded with reasons |
| Failure rates measured, not estimated, where telemetry allows | §4 — every figure carries its SQL or ES aggregation; §4.1 independently reproduces the ticket's 446/57%/$14.19 |
| Where a rate cannot be measured, say so | §5.1 — entity extraction's field-level failures are unmeasurable by construction, stated as such rather than estimated |
| Recommendation groups calls by whether a contract is appropriate, with reasoning per group | §8.3 (adopt, 5 sites, prioritised) and §8.4 (do not adopt, 7 sites, reason each) |
| Call sites are not fixed in this ticket | No file under `src/` is modified by this branch |

**Limitations of this audit, stated explicitly.** (a) The 14-day window covers a period in which
the digest was in an anomalous regime (§4.3) — the pre-07-23 baseline is only 58 calls, so
"the pattern was harmless for months" rests on a small sample within this window and on the
absence of prior incident reports, not on 14 days of high-volume evidence. (b) Reflection (A3/B1)
and the context compressor were partly or wholly disabled during the window, so their zero
failure counts mean "not exercised", not "healthy". (c) `main_inference` aggregates three call
sites under one `purpose`, so C1, D1 and A5 cannot be separated in the cost ledger; per-site
attribution there would need a `budget_role` split. (d) Semantic correctness is out of scope
throughout — this audit measures whether replies *parse*, never whether they are *right*.
