# FRE-1489 — the volatile block is duplicated into the head of the tool loop

**Ticket:** FRE-1489 (Approved, Tier-1, stream:build1)
**Backing ADR:** ADR-0081 (cache-aware context layout) — D1, D2, D4
**Related:** FRE-1487 (the study that found the plateau), ADR-0147, FRE-433

---

## 1. What the measurement actually shows

The ticket attributes the plateau to the volatile block landing at the **head** of the
sequence inside a tool loop. The measured divergence has a different cause, and the
evidence below refutes the 516-token memory-section hypothesis the ticket asked to test
before designing a fix.

### The divergence, observed at byte level (AC-1)

`scripts/research/fre1489_prompt_divergence.py` (step 6) drives the real `step_llm_call`
three times against one `ExecutionContext`, exactly as the tool loop does, captures each
call's wire messages through `build_wire_messages`, and renders them with the vendored Qwen
chat template (`slm_server/config/templates/qwen3.6-unsloth.jinja`). First differing byte
per pair, before the fix:

| Turn shape | call 0 → 1 | call 1 → 2 |
|---|---|---|
| Plain tool loop | at `len(prompt_0)` — strict forward extension | at `len(prompt_1)` — strict forward extension |
| HYBRID synthesis | 41 bytes **before** the end of prompt_0 | 279 bytes before the end of prompt_1 |

After the fix both shapes report `first_diff == len(previous prompt)` — the difference falls
where the model's own generation continues, which is what a forward extension means. The
HYBRID prompt also stops growing from the duplication: 12,308 chars on call 2 before, 11,815
after, on the instrument's fixture. Reverting the one-function change reproduces the
"before" row.

In the HYBRID shape the divergence sits at the **end of the first user message**, where a
second copy of the whole `<turn_context>` fence begins. The plain tool loop is byte-stable,
so the ticket's premise — that the head placement itself inverts the gradient — does not
hold: the head is byte-identical across calls when nothing rewrites it.

### The named component (AC-2)

Not one part of the block. The **whole** fenced block, plus the expansion synthesis
context, is duplicated per call:

| Call | `ctx.messages[0]` chars | `<turn_context>` fences | synthesis-context copies |
|---|---|---|---|
| 0 | 6,246 | 1 | 1 |
| 1 | 12,440 | 2 | 2 |
| 2 | 18,634 | 3 | 3 |

Mechanism, verified in code:

1. HYBRID expansion appends the synthesis context as a **second, adjacent `user`
   message** (`executor.py:5426-5434`).
2. `_inline_volatile_with_outcome` targets the last user message, so the fence (skill
   bodies + memory section + salient highlights + artifact note + datetime) rides that
   synthesis message.
3. `_validate_and_fix_conversation_roles` merges the two adjacent user messages — and
   merges **in place**: `prior["content"] = merge_content(...)` and the sibling
   `prior["tool_calls"] = …` write into the dict the caller still owns
   (`executor.py:1287-1292`). The fixer keeps the caller's own dict objects in its output
   list (`executor.py:1235-1248`, `1299-1300`), so the write lands in `ctx.messages`
   whenever the request list shares those dicts. It does whenever
   `llm_append_no_think_to_tool_prompts` is `False` (the production default,
   `settings.py:194`), which is when `request_messages` **is** `ctx.messages`
   (`executor.py:6450`). A heavy turn is not exempt: `_append_heavy_directive` builds a new
   outer list but copies no inner dict (`executor.py:1909`), so the write still reaches the
   turn's history.
4. Every later iteration re-merges the still-present synthesis message into the already
   merged first message, appending another copy.

ADR-0081 D2 point 2 predicted this exact failure: a separate `role:"user"` message next to
the query "would be merged/reordered by `_validate_and_fix_conversation_roles` … which
would perturb the frozen bytes". The expansion path introduces such a message.

### The production control (AC-4's harness-side half)

Every local primary loop since 2026-08-31 with three or more primary calls, split by
whether the turn ran expansion (`expansion_controller_complete`):

| Turn | Expansion | `cache_read_tokens` per primary call |
|---|---|---|
| `7c680135` | no | 10,026 · 18,319 · 19,647 · 21,095 · 22,919 · 25,435 · 30,506 · 35,680 · 39,799 |
| `cf25bc13` | no | 10,085 · 15,902 · 19,418 · 22,875 · 26,616 · 32,116 · 38,976 |
| `b65c92b7` | no | 10,085 · 12,491 · 14,519 · 17,815 · 19,283 · 23,259 · 29,198 |
| `1c14f2ed` | yes | 10,118 · 25,255 · **25,255** |
| `0a7e2c77` | yes | 0 · 16,321 · **16,321** · **16,321** |
| `ea8e12ee` | yes | 0 · 25,701 · **25,701** |
| `b7673af4` | yes | 0 · 26,571 · **26,571** · **26,571** |
| `94c2e86e` | yes | 10,085 · 15,673 · **10,085** · **10,085** · **10,085** |
| `94fda7de` | yes | 0 · 18,703 · **18,703** · **18,703** |
| `515625b3` | yes | 0 · 29,011 · **29,011** |
| `30224153` | yes | 0 · 10,085 · 24,470 · **24,470** |
| `d5e83932` | yes | 0 · 0 · 20,432 · **20,432** |
| `ef606e68` | yes | 0 · 0 · 36,724 · **36,724** |

No expansion, no plateau. Expansion, always a plateau once the loop reaches a third call —
and in `94c2e86e` reuse falls back to the static prefix. `b7673af4` carries the same defect
with no local worker call at all, which is why a search for `role: sub_agent` model calls
misses it: the synthesis message is appended whenever expansion completes, workers or not.

### What stays inferred

The gap between the observed divergence (about 6 tokens before the prompt end) and the
reported reuse (`prev_input − 516` in all three sessions) is the **server's** rollback
granularity. Every evidence session is a HYBRID turn, and the same-model sub-agent control
reuses `prev_input + prev_output` exactly, so the harness-side divergence is necessary and
sufficient to explain the plateau's existence. Its exact numeric floor is a llama.cpp
context-checkpoint property we cannot read from the VPS. This plan does not design against
it.

Sessions: 2e904079, 90f9d30e, af785d12 (primary, plateau) · 31e8f1a1 and the same turns'
workers (monotonic control).

## 1b. The injector matrix (codex plan-review finding 2)

Four paths append a message next to the query. Purity fixes one of them, and the review was
right that the plan overgeneralized. What each does, verified in code:

| Injector | Shape | With a pure fixer |
|---|---|---|
| Expansion synthesis (`executor.py:5426`) | second adjacent `user`, persistent | **fixed** — merged into a copy, so the carrier is byte-identical every call |
| Tool-budget warning (`executor.py:6246`) | new persistent `user` after tool messages | already a forward extension: it lands at the tail and takes its own fence. Fence count per request rises by design — the invariant must be per carrier, not global |
| Forced synthesis (`executor.py:6224`) | same shape, fires once | same — tail append, no earlier byte moves |
| Heavy directive (`executor.py:1909`) | transient trailing `user`, never persisted | **not fixed, deliberately out of scope** — see below |
| Attachment block-list content | block list, idempotent fence check (`executor.py:2563-2572`) | purity sufficient; `merge_content` already returns a new list |

**Heavy directive — scoped out, with the reason.** The directive is appended to every
request but persisted in none, and a tool message between it and the query blocks the merge
from the second call onward (`executor.py:1261-1274`). So call 1 sends `query+directive` as
one message while call 2 sends `query` alone: the head moves once, costing one re-prefill,
and each later call ends with about 90 tokens the next call does not reproduce. That is
bounded and monotonic — it does not produce a plateau. It is also ADR-0138 D5's territory
(D5 chose non-persistence on purpose, for reasons this ticket must not overturn in passing),
and no evidence session ran heavy: every plateau turn's later calls end on a tool message.
The handoff names it for a follow-up ticket rather than folding a grounding-design change
into a cache fix.

Note the mutation was accidentally *masking* this on heavy turns: writing `query+directive`
into `ctx.messages[0]` made call 2's head match. Purity removes that accident, which is the
correct trade — a clean history over one hidden re-prefill — but it must be stated, not
discovered later.

## 2. The fix

Make `_validate_and_fix_conversation_roles` pure. It is documented as a request-shaping
pass and every caller treats it as one; only the merge branch writes through.

Not chosen, and why:

- **Stop appending the adjacent synthesis message** (restructure the expansion path) —
  larger diff in a path with its own ACs (ADR-0149/0150 trailers, fan-out pauses), and it
  would leave the in-place merge free to bite the tool-budget and heavy-directive
  injectors, which also append a user message adjacent to the query.
- **Copy in the caller** — hides the defect at one of the call sites instead of removing it.

## 3. Steps

1. **Failing test first** — `tests/personal_agent/orchestrator/test_fre1489_volatile_duplication.py`:
   drive `step_llm_call` three times over a HYBRID-shaped `ctx` and assert
   (a) every call's wire messages forward-extend the previous call's — each earlier message
   byte-identical, appends only; (b) the **carrier** message holds exactly one
   `<turn_context>` fence on every call (per carrier, not per request — the tool-budget and
   forced-synthesis injectors legitimately add tail fences); (c) `ctx.messages[0]` still
   holds the user's own query verbatim after every call.
   → verify: fails on `main`, where call 1's carrier holds 2 fences and `ctx.messages[0]`
   has grown.
2. **Fix** `_validate_and_fix_conversation_roles` (`executor.py:1276-1292`): merge into a
   copy of the prior message, covering **both** the `content` write and the `tool_calls`
   write; state the purity contract in the docstring.
   → verify: step 1's test passes.
3. **Purity unit tests** (codex finding 4 — the integration test alone would miss a fix that
   copies `content` but still writes `tool_calls` through): three direct cases over
   `_validate_and_fix_conversation_roles` — string+string user merge, assistant merge whose
   incoming message carries `tool_calls`, and block-list content merge. Each asserts the
   input dicts are unchanged **and** the returned message carries the merged result.
4. **AC-5 test** — with a populated `memory_context`, the rendered memory text appears in
   the wire messages on every call of the loop. The cheapest way to pass AC-3 is to stop
   sending the block; this forbids it.
5. **AC-6 test** — a turn whose last message is a user turn keeps the fence on that turn's
   own message, and the plain tool loop stays a forward extension (the control that must not
   regress). Existing single-call coverage: `tests/test_orchestrator/test_frozen_layout.py`.
6. **The AC-1 instrument** — `scripts/research/fre1489_prompt_divergence.py`: render the
   captured calls through a chat template passed as an argument, locate the first differing
   byte, and **exit nonzero unless it equals the previous prompt's length** (codex finding 5:
   an instrument that only prints can report a divergence and still pass). This produced §1's
   divergence table and re-runs it.
7. **Quality gates** — `make test`, `make mypy`, `make ruff-check`, `make ruff-format`,
   `pre-commit run --all-files`; self-review per the build skill.

## 4. Acceptance criteria mapping

| AC | How this plan discharges it | Who observes |
|---|---|---|
| AC-1 divergence observed, not inferred | §1 table, produced by the step-6 instrument over the real assembly | this PR |
| AC-2 cause attributed to a named component | §1: the whole fence plus synthesis context, duplicated by the in-place merge; not the 516-token memory section | this PR |
| AC-3 primary cache advances monotonically | forward-extension test (step 1) is the deterministic proxy; the live `cache_read_tokens` curve is master's post-deploy check | master, post-deploy |
| AC-4 measured against the worker control | same live run: primary final-round hit rate against the turn's sub-agents | master, post-deploy |
| AC-5 volatile content still reaches the model | step 4 | this PR |
| AC-6 ADR-0081 single-turn behaviour preserved | step 5 | this PR |

AC-3's harness-side half also has a production control now: §1b's split shows nine-call
monotonic advance on turns without expansion against a frozen plateau on every turn with it.
The live curve remains master's to read.

AC-3 and AC-4 assert against a live multi-round local turn. They are not dischargeable
from this branch, and the handoff says so rather than claiming them.

## 5. Risk

Diff class: self-serve. One function in the request-shaping path, no schema, no cost or
governance code, no production write path. The behavioural change is that the merged
message is a copy — the wire bytes of a single call are unchanged, which is why the
existing role-fixer regression tests (`test_executor.py:1327-1504`,
`test_within_session_compression.py:506`, `test_content_widening.py`) must stay green
untouched.

Secondary effect worth naming in the handoff: the duplication also inflated the prompt by
one copy of the block plus the synthesis context per iteration (each of three workers'
reports measured 7,506 chars in session 2e904079), so the fix removes prefill the model
never needed as well as restoring reuse.
