# FRE-1360 — Untrusted input reaches the model only through a tool-result channel

**Ticket:** FRE-1360 (ADR-0140 T2, AC-4) · **Design intent:** ADR-0140 T2 (input integrity is
untrusted) · ADR-0081 D2/D3/D4 (frozen append-only layout, volatile content nearest the query) ·
**Tier:** Complex (message assembly in `src/`, memory, prompt cache) → codex plan-review required.

## Problem

`memory_section` (rendered knowledge-graph and episodic recall) joins `_volatile_block` in
`step_llm_call` (`executor.py:7101-7111`). The block is inlined as a `<turn_context>` fence into
the current user message. Graph content is agent-writable, so the least trusted input arrives as
user text.

A second path has the same defect. On HYBRID turns, `step_init` appends the worker reports
(`expansion_result.synthesis_context`, built from pages and tool output the workers read) as a
`role: "user"` message (`executor.py:5645-5655`). Owner decision 2026-10-10: fix this path in
this PR too.

## Design

### The channel

A **harness tool exchange** is one assistant message with one `tool_calls` entry, followed by
one `role: "tool"` message that answers it. The harness writes both. The model did not call the
tool. Real tool results already use this exact shape (`executor.py:8076-8083`), so every provider
path and the sanitiser already accept it:

- Anthropic: litellm merges the tool message into a user turn as a `tool_result` block.
  `claude-sonnet-5` made 105 tool-calling primary calls in the last 90 days with assistant
  `tool_calls` that carry no thinking blocks, and the logs show zero thinking-block errors.
  History tool names need not be in the request's tool list (the FRE-484 `noop` placeholder
  already relies on this).
- Local llama.cpp (Qwen, Gemma) and OVH: native tool role in the chat template. Every chat
  model in `config/models.yaml` resolves to `native` tool calling.
- `sanitise_messages` is a no-op on a matched pair.

Call ids: `call_mem_<trace_id[:31]>` (memory) and `call_wrk_<trace_id[:31]>` (worker reports).
The ids are unique per turn, deterministic within the turn, and at most 40 characters (the
OpenAI id limit). Tool names: `memory_recall` and `worker_reports`. Neither is a registered
tool. If the model calls one, dispatch returns the normal unknown-tool error.

Content is the existing bytes, unchanged: `memory_section` exactly as the renderer produced it
(including the ADR-0148 state line), and `synthesis_context` exactly as the expansion
controller built it. No new wrapper text.

### What moves, and what stays

| `_volatile_block` member | Source | Decision |
|---|---|---|
| `memory_section` | knowledge graph + episodic recall (agent-writable) | **Moves** to the `memory_recall` tool result |
| `_skill_bodies_tail` | skill files in the repo (harness-authored) | Stays in the fence |
| `ctx.salient_highlights` | the session's own compaction recap | Stays in the fence. It is a summary of this conversation, not one of T2's four source classes. The recap message itself is already an assistant message. |
| `artifact_builder_planning_note` | static template | Stays in the fence |
| `_current_datetime_block` | clock | Stays in the fence |

### Placement and order (ADR-0081 §D3/§D4, AC-3)

Single turn, first primary call:

```
… history … | user: <turn_context>skills · highlights · note · datetime</turn_context> + query
            | assistant: tool_calls=[memory_recall]
            | tool: memory_section
```

HYBRID turn, first primary call:

```
… history … | user: query
            | user: <turn_context>…</turn_context> + synthesis instruction (role fixer merges these two, as today)
            | assistant: tool_calls=[worker_reports] | tool: synthesis_context
            | assistant: tool_calls=[memory_recall]  | tool: memory_section
```

Memory is the last context before the model generates, directly after the query (single turn).
Before this change, memory sat in the fence directly before the query, with highlights and the
note between it and the query. The memory items, their order and their bytes do not change.

The exchange is appended in the same once-per-turn branch that inlines the fence (FRE-1529), and
only when that branch lands (`ctx.turn_context_inlined` becomes true). Later rounds never append
it again. The messages are never rewritten afterwards, so the sequence stays a forward extension
(ADR-0081 D2).

### Prompt-cache consequence (AC-4)

- Message 0 (the system prompt) does not change. The cached system prefix is unchanged.
- The Anthropic history-end breakpoint keys on the last user message. That is still the carrier,
  so the breakpoint does not move.
- The memory bytes were already in the per-turn new region. They now sit after the query instead
  of before it. Both positions are after every cached prefix.
- Net cost: the synthetic assistant tool call adds about 20–40 tokens per turn.
- Expected effect on cached-prefix hit rate: neutral. Measured after deploy (master reads AC-4).

### Supporting changes (fold-ins)

1. **Turn evidence admission.** Memory admission today requires the fence on this turn's user
   message (`turn_evidence.py:823`). Memory now has its own channel, so admission uses its own
   check: the wire carries a `role: "tool"` message whose `tool_call_id` is this turn's
   `memory_recall` id. The sanitiser removes orphan tool messages, so presence in the wire form
   implies a matched pair. `memory_section` leaves `_VOLATILE_TAIL_COMPONENTS` and is filtered on
   the new check. Skill bodies keep the fence check. Without this change, memory sources never
   register for citation (ADR-0138 D2 item 1).
2. **Context-retry trim exemption.** `_trim_messages_for_context_retry` stubs every tool result
   except the newest. Harness tool results (memory, worker reports) are exempt, because the old
   carriers (fence, user message) were never stubbed.
3. **HYBRID synthesis message.** `step_init` appends the instruction as a user message, then the
   worker-reports exchange. The instruction text changes from "Synthesize the results…" to
   "The worker reports follow as a tool result. Synthesize them into a coherent response for
   the user's original question."

4. **HYBRID planner call** (Codex finding 1). The planner's single user message carries the
   ADR-0154 D5 memory digest (`expansion_controller.py:371-386`, `:1155-1158`). The digest
   moves to a `memory_recall` exchange after the planner user message. The ADR-0154 D1 bound
   still counts the digest. `build_planner_user_message` returns the digest separately, and a
   new `planner_request_messages()` builds the four messages. `scripts/eval/fre1537/render.py`
   `build_planner_request` uses the new helper, so the probe still qualifies what ships.
   Probe on the live local model (owner-approved, 2026-10-10): a request with no tool list and a
   200-fact `memory_recall` exchange returned no error and 944 prompt tokens, so the template
   renders tool history without a tool list.
5. **Planner history render** (Codex finding 2). `planner_history_text` renders every role as
   `"role: content"` text in a user message. It now skips `role: "tool"` messages and assistant
   messages that carry only `tool_calls`. This also stops real tool results of reused CLI
   sessions from reaching the planner as user text.

### Codex plan-review dispositions (2026-10-10)

| # | Finding | Disposition |
|---|---|---|
| 1 | Planner memory digest is user text | **Fixed** — fold-in 4. |
| 2 | Persisted exchanges flattened into planner history | **Fixed** — fold-in 5. |
| 3 | Renderer registers memory sources before admission | **Not changed.** Pre-existing: `_render_memory_section_with_ids` registers each item to mint its citation id (`executor.py:3958`) before this change too. Recorded in the handoff. |
| 4 | Trailing exchange buries the Qwen `/no_think` suffix | **Not changed.** `llm_append_no_think_to_tool_prompts` defaults to `False`, and real tool loops already put assistant and tool messages after the suffixed user message. |
| 5 | Anthropic call with tool history and no `tools` | **Covered by litellm** — `AnthropicConfig.transform_request` injects a dummy tool when `tools` is absent and the history has tool blocks (litellm 1.98, `llms/anthropic/chat/transformation.py:1798`). A test pins that behaviour on our exact exchange. |
| 6 | Compaction recirculates tool content into salient highlights | **Not changed.** Every real tool result already takes this path. The recap is a model summary of the session, a derived-content question for T2 as a whole. Recorded in the handoff. |
| 7 | 16-character id prefix | **Fixed** — the id keeps 31 characters of the trace id (40 total). |
| 8 | Proof-table gaps | **Fixed** — tests added for the planner digest, planner history, the Anthropic transform, both trim exemptions, a non-populated memory state, role fixer + sanitiser over the HYBRID sequence, the Anthropic cache breakpoints, and a second turn replayed over persisted history. Qwen suffix: not tested (setting off). |

### Out of scope

- `operator_stanza` (owner name from `:Person` facts) sits in the system prompt. Owner decision
  2026-10-10: file a separate ticket.
- PDF attachment text in the user message: user-supplied, not one of T2's four classes.

## Steps

Each step is TDD: write the test, run it, see it fail, implement, see it pass.

1. **New module `src/personal_agent/orchestrator/untrusted_channel.py`.**
   - `MEMORY_RECALL_TOOL = "memory_recall"`, `WORKER_REPORTS_TOOL = "worker_reports"`.
   - `harness_call_id(kind: Literal["mem", "wrk"], trace_id: str) -> str`.
   - `harness_tool_exchange(*, call_id: str, tool_name: str, content: str) -> tuple[dict, dict]`.
   - `is_harness_tool_result(message: Mapping[str, object]) -> bool` (id prefix `call_mem_` or `call_wrk_`).
   - Tests: `tests/personal_agent/orchestrator/test_untrusted_channel.py`.
2. **`types.py`**: `ExecutionContext.memory_result_call_id: str | None = None`.
3. **`executor.py` `step_llm_call`**: drop `memory_section` from `_volatile_block`. In the
   once-per-turn branch, after the inline: if `memory_section` and `ctx.turn_context_inlined`,
   append the memory exchange to `ctx.messages` and set `ctx.memory_result_call_id`. Update the
   ADR-0081 comment block.
4. **`executor.py` `step_init` (HYBRID)**: instruction as user message, then the worker exchange.
5. **`executor.py` `_trim_messages_for_context_retry`**: exempt harness tool results.
6. **`turn_evidence.py` + `_record_turn_evidence`**: new parameter `memory_result_call_id`,
   new check `_wire_carries_tool_result`, admission and component filter as above.
7. **Update existing tests** that assert memory inside the fence (`test_frozen_layout.py`,
   `test_fre1489_volatile_duplication.py`, `test_fre1529_volatile_once_per_turn.py`,
   `test_content_widening.py`, `test_adr_0126_*.py`, `test_identity_precedence.py`,
   `test_turn_evidence.py`, `test_prompt_layout_order.py`, and others that fail). Each edit keeps
   the test's intent and changes only where it looks for memory.
8. **Docs**: ADR-0081 D2 point 2 gets a dated note (recalled memory no longer rides the fence).
   ADR-0140 AC-4 gets a dated note (closed by FRE-1360). Executor comments.
9. **File the operator-stanza ticket** (Needs Approval, PersonalAgent).

## Acceptance-criteria proof

New file `tests/personal_agent/orchestrator/test_fre1360_untrusted_input_channel.py`.

| AC | Test | Asserts |
|---|---|---|
| AC-1 | `test_ac1_recalled_memory_marker_reaches_only_a_tool_result` | Seeded memory with a unique marker, real `step_llm_call`, wire form from `build_wire_messages`. The marker is in no system message and no user message, in exactly one `role: "tool"` message, and that message answers an assistant `tool_calls` entry with the same id. |
| AC-2 tool results | `test_ac2_native_tool_result_marker_reaches_only_a_tool_result` | Full turn through `Orchestrator.handle_user_request`, real `ToolExecutionLayer` and `ToolRegistry`. A native tool returns the marker. Every `respond()` call is captured. |
| AC-2 web content | `test_ac2_fetched_web_content_marker_reaches_only_a_tool_result` | Same harness. The real `fetch_url` tool runs, with its HTTP layer patched to return a page that holds the marker. |
| AC-2 MCP | `test_ac2_mcp_response_marker_reaches_only_a_tool_result` | Same harness. A real `MCPGatewayAdapter` registers a tool from a fake MCP client whose `call_tool` returns the marker. |
| AC-2 HYBRID | `test_ac2_worker_report_marker_reaches_only_a_tool_result` | A HYBRID synthesis context that holds the marker, appended by the real step-init code, then the real `step_llm_call`. |
| AC-2 count | `test_ac2_every_declared_class_is_probed` | The four T2 classes each map to at least one probe test in this module. |
| AC-3 | `test_ac3_recall_items_same_set_same_order` | Fixed memory context with three episodes. The episode ids appear on the wire in the same order as a baseline recorded by running the same extraction on `main` before the change. The tool content equals the renderer output byte for byte. The evidence record admits the same identities in the same order. |
| AC-3 | `test_ac3_memory_is_the_context_nearest_the_query` | The memory exchange directly follows the carrier user message. |
| AC-3 | `test_ac3_wire_stays_a_forward_extension_across_rounds` | Three tool-loop rounds. No call rewrites an earlier message, and the memory exchange appears once. |
| AC-4 | post-deploy, master | ES query in the handoff: `cache_read_tokens / input_tokens` for `prompt_callsite: orchestrator.primary`, per model, seven days before and after deploy. |

## Commands

```bash
make test-file FILE=tests/personal_agent/orchestrator/test_untrusted_channel.py
make test-file FILE=tests/personal_agent/orchestrator/test_fre1360_untrusted_input_channel.py
make test && make mypy && make ruff-check && make ruff-format && pre-commit run --all-files
```

## Risks

- **A model calls `memory_recall` itself.** Dispatch returns an unknown-tool error and the loop
  continues. Visible in telemetry as an unknown-tool call.
- **Turn-evidence regression.** Covered by step 6 and its tests. A wrong admission check means
  memory sources stop registering for citation, which the existing ADR-0138 tests catch.
- **CLI sessions with a reused `SessionManager`** persist the exchanges in history. The HTTP path
  stores only raw user text and the final answer, so production history does not carry them.
