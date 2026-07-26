# FRE-996 — Pilot: a JSON output contract for the session digest

**Ticket:** FRE-996 (Approved, Tier-1:Opus, stream:build1) · **Backing:** ADR-0124 (D3, Amendment B)
**Blocked by:** FRE-995 (merged, `0c0b6330`) · **Audit:** `docs/research/modeled_output_contract_audit_2026-07-26.md`
**Authoritative framing:** the master gate correction comment on FRE-996 (2026-07-26) **overrides the
ticket description** where they conflict. This plan is written against the corrected framing.

---

## 1. What the hypothesis actually is (corrected), and which mechanism carries it

The ticket's original hypothesis — "unparseable replies stop occurring, and the class goes to zero" —
**cannot succeed and is not what this pilot tests.** Master's correction two, and audit §6.3, both say
so. I verified the mechanism against the installed litellm source rather than inheriting the claim,
and a codex adversarial review then falsified part of my own first reading. Both corrections are below.

### 1.1 `response_format` is the wrong mechanism for this stack — rejected

| Fact | Evidence |
|---|---|
| The deployed digest model is `claude-sonnet-5` | `config/model_roles.yaml:60` → `config/models.yaml:144`, provider `anthropic` |
| litellm 1.89.2 routes `response_format` to Anthropic's **native `output_format`** only for a hardcoded model-substring allowlist — `sonnet-4.5/4-5/4.6/4-6`, `opus-4.1/4-5/4-6/4-7`. **`claude-sonnet-5` matches none of them.** | `litellm/llms/anthropic/chat/transformation.py:1467` |
| So `response_format` becomes a **forced synthetic tool**: `json_tool_call` + `tool_choice`, with `json_mode=True` | `transformation.py:1492`, `:1585`; reproduced by running the mapper offline |
| With `json_mode`, litellm converts the tool arguments back into `message.content` and clears `tool_calls` | `_resolve_json_mode_non_streaming`, `transformation.py:2038` |
| **litellm then overwrites the provider's `stop_reason` with `"stop"` whenever a json-mode message exists — and derives `finish_reason` from the overwritten value** | `transformation.py:2485` |

That last row is disqualifying, and it is the finding codex caught that I had missed. Under
`response_format`, **a truncated reply reports `finish_reason == "stop"`.** The pilot's entire
measurement rests on separating truncation from drift; a mechanism that destroys the truncation
signal cannot be the mechanism under test. `response_format` is therefore **rejected**.

### 1.2 Chosen mechanism — an explicit tool, which keeps the stop reason intact

Passing `tools=` + `tool_choice=` ourselves (the other option the ticket names) was verified offline
against the same mapper:

- litellm maps it to a native Anthropic tool + `tool_choice={"type":"tool","name":…}`, and **does not
  set `json_mode`** — so `stop_reason` survives and `finish_reason` is truthful (`"length"` on truncation).
- The payload arrives as a real `tool_calls` entry, which `LiteLLMClient` already normalises.
- We control the schema exactly; nothing is rewritten under us.

### 1.3 What the contract does and does not enforce — claim weakened deliberately

Codex's second finding, also verified: litellm's synthetic tool carries **no `strict: true`**
(`transformation.py:1585`), and on the explicit-tool path litellm **drops** a `strict` key we set
ourselves (confirmed offline — the mapped Anthropic tool contains only `name`/`description`/
`input_schema`/`type`). So **Anthropic's strict tool use is not reachable through litellm 1.89.2 by
either route.** This qualifies audit §8.1's "capability is already wired" and is itself a reportable
finding.

Therefore, precisely:

| Property | Status |
|---|---|
| Fence wrapping / trailing prose | **Eliminated by mechanism.** The payload travels in `tool_use.input`, a structured field. There is no free text to unwrap, so the failure has nowhere to occur. This claim is airtight. |
| Key-name / shape drift, enum drift | **Strongly constrained, not formally guaranteed.** Schema-guided, not schema-enforced, absent `strict`. These stay **measured** classes; no zero is claimed on mechanism. |
| Truncation | **Not eliminated.** Persists at whatever rate the ceiling dictates — but now attributable, because this route preserves `finish_reason`. |

**Restated hypothesis (the one this pilot tests):**

> Under an enforced tool contract, fence/trailing-prose wrapping goes to zero *by mechanism*. Shape
> and enum drift are expected to fall sharply but are measured, not assumed. Truncation persists and
> becomes loudly attributable instead of masquerading as a parse error.
>
> A run in which wrapping is zero, drift is sharply reduced, and truncation persists is a **success**
> for this ticket and is the expected result.

---

## 2. Scope

**In:** a wire model distinct from the storage model; the contract wired into the digest producer
behind a setting; truncation made a first-class, separately-countable failure; an out-of-band A/B/C
harness over the real capture corpus; the results write-up.

**Out (explicitly):** sizing / the token bound (FRE-993, FRE-994); generalising the contract to the
other four Group-A sites (FRE-995 §8.3 sequences that, and this ticket does not decide it); the
producer staying disabled is unchanged by this work.

---

## 3. Deliverables

### D1 — Wire model · `src/personal_agent/memory/session_digest_wire.py` (new)

Audit §8.2 is a blocker on using `SessionDigest.model_json_schema()` directly: `UnresolvedItem.as_of`
is **producer-stamped** (ADR-0124 D3, stamped at `session_summary.py:418` from `ended_at`), so handing
the storage schema to the provider would ask the model to author a field the ADR reserves. A separate
wire model is therefore required, not preferred.

```
WireLocator      capture_id: str · field: Literal["assistant_text"]
WireItem         text: str · basis: BasisTag
WireCorrection   text · basis · tier: CorrectionTier · span: str · locator: WireLocator
                 · evidence_span: str · evidence_locator: WireLocator
WireDigest       established / decisions / unresolved / corrections : list[...]
DigestEnvelope   label: str · digest: WireDigest
```

Deliberately excluded from the wire, each with a stated reason:

| Excluded | Why |
|---|---|
| `UnresolvedItem.as_of` | Producer-stamped (ADR-0124 D3). The whole reason a wire model exists. |
| `span` / `locator` on `established` / `decisions` / `unresolved` | Amendment B retired `tool_evidence`, the only basis that ever obliged a citation outside `corrections`. The current prompt's `item` shape (`session_summary.py:124`) already asks only for `text` + `basis`. Carrying them as nullable fields would add optionality to a strict schema for no gain. |

Tightened *relative to the prose prompt*, because a contract can enforce what prose only asks:
`field` becomes `Literal["assistant_text"]` (today `_parse_locator` accepts any string and the span
check fails later), and `span`/`evidence_span`/`evidence_locator` are **required** on `WireCorrection`
(today `_parse_correction` raises if they are absent — same rule, moved to the decoder).

Functions:
- `to_storage(envelope, *, ended_at) -> tuple[str, SessionDigest]` — the producer-side half: stamps
  `as_of`, enforces `MAX_LABEL_CHARS` **in Python** (audit §8.2 — the dialect has no `maxLength`).
- `digest_tool(*, bounded: bool = False) -> dict` — builds the OpenAI-format **tool definition**
  (§1.2), not a `response_format`. Post-processes Pydantic's schema: rewrites single-value `const` →
  `enum: [v]` (broader provider support; Pydantic v2 emits `const` for a one-value `Literal`).
  `bounded=True` additionally sets `maxItems` per slot — **harness-only**, see D4 arm C.
- `DIGEST_TOOL_NAME` / `digest_tool_choice()` — the forced-selection payload.

### D2 — `finish_reason` surfaced · `llm_client/types.py`, `llm_client/litellm_client.py`

`LLMResponse` currently drops `finish_reason` (it is only reachable by digging into `raw`). Add
`finish_reason: NotRequired[str | None]`; populate from `choice.finish_reason` in `LiteLLMClient`.
`LocalLLMClient` leaves it absent — `NotRequired`, so no caller breaks and no unverified claim is made
about the local path.

This is a supporting change folded in per build SKILL §5: without it the pilot cannot tell truncation
from drift, which is the one distinction the ticket requires.

### D3 — Producer wiring · `second_brain/session_summary.py`, `config/settings.py`

1. New setting `session_digest_structured_output: bool = True`. Reversible, observable-first.
2. `_call_model` passes `tools=[digest_tool()]` + `tool_choice` **on the cloud path only**, when the
   setting is on, and reads the payload from `tool_calls[0].arguments`, falling back to `content` when
   the model answered in text anyway. The local path is left alone and commented: llama-server's tool
   and `json_schema` handling is outside this pilot's evidence, and the deployed role is cloud sonnet.
   No unverified claim ships.
3. `_call_model` returns `(content, finish_reason)`.
4. New `SummaryFailureReason.OUTPUT_TRUNCATED`, **added to `TERMINAL_ELIGIBLE_REASONS`.** Truncation
   currently lands in `SCHEMA_INVALID`, which *is* terminal-eligible (`session_digest.py:120`).
   Precisely (codex correction, verified): the two-attempt loop inside `generate_session_digest`
   (`session_summary.py:88`, `:585`) bounds *one invocation* either way; what terminal-eligibility
   bounds is the **cross-sweep** retry — `summary_attempt_count` is incremented once per failed
   invocation (`memory/service.py:1521`) and a dirty session is only excluded once its latest reason
   is terminal-eligible *and* the count reaches `max_attempts` (`:1627`). Omitting the new reason from
   the set would therefore leave the session eligible for sweeps indefinitely. This is a re-labelling
   that preserves current behaviour, not a behaviour change.
5. `finish_reason` added to the `session_summary_failed` and `session_summary_generated` events.

The prose JSON shape stays in the system prompt. Removing it would change two variables at once and
make the A/B uninterpretable.

### D4 — Pilot harness · `scripts/eval/digest_contract_pilot.py` (new)

Out of band. **The producer stays disabled** — the harness calls the model directly and never calls
`generate_session_digest`, so the `session_summary_enabled` gate is not touched.

- **Corpus:** the durable captures index (`agent-captains-captures-*`, cloud-sim ES), ~2,856 real
  captures. Sessions with `>= MIN_TURNS_FOR_DIGEST` turns.
- **Sample:** deterministic (sorted by session id, seeded), so all three arms run on the **identical**
  sample. Default N = 30 sessions.
- **Arms:** A = today (no contract) · B = tool contract · C = tool contract + bounded schema (`maxItems`).
- **Confound controls** (codex finding 5, accepted):
  - `temperature=0.0` set **explicitly on every arm**. Today `_call_model` omits it and the client
    forwards it only when non-`None` (`litellm_client.py:414`), so arms would otherwise differ by
    sampling noise as much as by mechanism.
  - Arms **interleaved per session** (A,B,C for session 1, then session 2 …), not arm-major, so a
    provider drift or outage mid-run hits all arms equally rather than one.
  - Input tokens and cache read/write recorded per arm and **reported separately**: B and C send a
    tool definition, which Anthropic bills as input and which shifts prompt-cache reuse. B/C are
    expected to cost more per call than A, and that is a property of the mechanism, not noise.
  - The harness measures **per-call** rates. Production applies up to two attempts per invocation
    (`session_summary.py:88`), so production final-outcome rates are strictly better than these.
    Stated in D5; no per-call rate is presented as a production rate.
- **Per call, recorded:** `finish_reason`, input/output tokens, cache tokens, content length, cost,
  and a *mechanically* classified outcome:

  | Class | Rule |
  |---|---|
  | `truncated` | `finish_reason` in `{length, max_tokens}` **OR** `completion_tokens >= max_tokens` |
  | `ok_at_ceiling` | parsed **and** valid, but `completion_tokens >= max_tokens` — **never counted as clean** |
  | `wrapping` | payload arrived as free text needing fence/trailing-prose removal |
  | `invalid_json` | unparseable and **not** attributable to truncation by either signal above |
  | `shape_drift` | parses as JSON but `parse_model_output` rejects the shape (missing key, wrong type) |
  | `enum_drift` | rejected specifically on `basis` / `tier` / locator `field` |
  | `empty` · `provider_error` · `ok` | as named |

  The `truncated` rule deliberately carries **two independent signals**. `finish_reason` alone is
  fragile — §1.1 showed litellm can overwrite it — so a token-count corroboration means a future
  library change cannot silently reclassify truncation as success. `ok_at_ceiling` exists because the
  cheapest false success is a digest truncated *inside a list* that still parses as a valid, shorter
  envelope; without this class it would be scored `ok`.

  Plus `digest_token_count` of the rendered digest, giving FRE-994 a length distribution measured on
  a pipeline that is not dominated by truncation.
- **Spend control:** prints the estimate and **exits** unless `--confirm-spend` is passed. Accumulates
  actual cost and reports it against the estimate.
- **Budget lane:** calls reserve against the `captains_log` role. Headroom is checked *before*
  proposing the run and `BudgetDenied` is surfaced verbatim. **No cap is changed** — per standing
  instruction, a cap is never raised to make a run fit.

### D5 — Results · `docs/research/digest_contract_pilot_2026-07-26.md`

Before/after table per class with the sample size stated; the length distribution; the bounded-schema
result **reported separately**; and the mechanism argument in §1 stated as the primary evidence, with
the sample as corroboration rather than proof.

### D6 — Tests

`tests/personal_agent/memory/test_session_digest_wire.py`
- `as_of` appears **nowhere** in the emitted schema (walks `$defs` too) — proves the D3 non-violation.
- `to_storage` stamps `as_of == ended_at` on every unresolved item.
- Equivalence: a valid envelope through `to_storage` == the same JSON through `parse_model_output`.
- Every object carries `additionalProperties: false`; `basis`/`tier`/`field` are `enum`s; **no
  `maxLength` anywhere** (audit §8.2).
- `bounded=True` adds `maxItems` and changes nothing else.
- **Mechanism pin (offline, free):** the tool survives litellm's Anthropic mapping for
  `claude-sonnet-5` as a native tool + forced `tool_choice`, and **`json_mode` is not set**. This is
  the property §1.2 rests on — a litellm upgrade that starts setting `json_mode` (and so overwrites
  `stop_reason`) fails this test loudly instead of silently invalidating the measurement.

`tests/personal_agent/second_brain/test_session_summary_contract.py`
- `tools` + `tool_choice` are passed when the setting is on, omitted when off.
- The payload is read from `tool_calls[0].arguments`, with a text `content` fallback.
- `finish_reason == "length"` → `OUTPUT_TRUNCATED`, not `SCHEMA_INVALID`.
- `OUTPUT_TRUNCATED` is in `TERMINAL_ELIGIBLE_REASONS`.
- Existing `session_summary` tests still pass unchanged.

---

## 4. Steps

| # | Step | Verify |
|---|---|---|
| 1 | Failing tests for D1 (wire model) | `make test-file FILE=tests/personal_agent/memory/test_session_digest_wire.py` → fails on import |
| 2 | Implement D1 | same → passes |
| 3 | Failing tests for D3 (contract + truncation) | `make test-file FILE=tests/personal_agent/second_brain/test_session_summary_contract.py` → fails |
| 4 | Implement D2 + D3 | same → passes; existing digest tests still green |
| 5 | Harness D4 | `--dry-run` lists the sample + cost estimate, spends nothing |
| 6 | **Owner gate:** state estimate, confirm the `captains_log` lane has headroom, get explicit go | owner says go |
| 7 | Run A/B/C | results JSON written; actual cost reported against estimate |
| 8 | Write D5 | doc committed |
| 9 | Quality gates + self-review | `make test` · `make mypy` · `make ruff-check` · `make ruff-format` · `pre-commit run --all-files` · code-review (high — src logic) + security-review |

---

## 5. Acceptance criteria and how each is proven

| # | Criterion | Proof |
|---|---|---|
| AC1 | The digest call carries a schema contract, using existing client support (no plumbing change) | D3 + test that `tools`/`tool_choice` reach `respond`; audit §8.1 confirms the client already forwards both |
| AC1b | The mechanism is stated honestly: what it enforces vs. merely encourages | §1.3 — wrapping eliminated by mechanism; shape/enum measured, because `strict` is unreachable via litellm 1.89.2 |
| AC2 | The contract contains **only model-authored fields** — no ADR-0124 D3 violation | D6 test: `as_of` absent from the whole schema incl. `$defs` |
| AC3 | Before/after on the **same** sample, sample size stated | D4 deterministic sample; D5 table |
| AC4 | Wrapping / shape drift / enum drift reported **separately from** truncation; truncation **not** claimed eliminated | D4 classifier; D5 reports them in separate rows |
| AC5 | The elimination claim rests on a **mechanism argument**, not a clean sample | §1 of this plan, carried into D5 with the litellm/Anthropic trace |
| AC6 | Output-length distribution reported for FRE-994 to inherit | D4 records `digest_token_count` + output tokens per call |
| AC7 | The bounded-schema result is a **separate** finding | D4 arm C; D5 separate section |
| AC8 | Expected cost stated before the run, actual against it after | D4 `--confirm-spend` gate; D5 |
| AC9 | The 90-char label bound stays a Python check | `to_storage`; D6 test asserts no `maxLength` in the schema |

---

## 6. Risks

| Risk | Handling |
|---|---|
| **Silent false success** — a digest truncated inside a list still parses as a valid shorter envelope and scores `ok` | The `ok_at_ceiling` class (D4) plus the two-signal truncation rule. This was codex's "cheapest misleading result" and is now structurally excluded rather than hoped against. |
| Anthropic **rejects** unsupported keywords (`maxItems`) in a tool `input_schema` | Arm C is isolated; a 400 is itself the finding and is reported as such. Arms A/B are unaffected. |
| `$ref`/`$defs` in a tool `input_schema` behave differently than expected | Arm B's first call surfaces it immediately; falling back to a flattened schema is a small, contained change |
| The `captains_log` budget lane denies the run | **Live constraint, not hypothetical:** the daily cap is $5.00 (`config/governance/budget.yaml:53`) and today's spend is already $3.67, leaving ~$1.33. Surfaced to the owner *before* the run; **no cap is raised** — the owner chooses a smaller N, a later day, or defers. |
| A litellm upgrade changes the mechanism under us (e.g. starts setting `json_mode` on the tool path, or moves sonnet-5 onto the native `output_format` allowlist) | D6 pins both properties with an offline test that fails loudly |
| Cost-gate reservation under-estimates B/C (the estimator sees only messages + `max_tokens`, not tools — `litellm_client.py:466`) | Noted, not fixed here: it under-reserves rather than over-reserves, so it cannot block the run. Reported as a follow-up. |
| Sample too small to separate a low-rate class | N and the resulting confidence stated plainly in D5; no rate is claimed as zero on mechanism-free evidence |
