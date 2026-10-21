# FRE-1647 — the forced-tool producers send `auto` where a deployment cannot force a tool

Ticket: FRE-1647. Background: FRE-1621 note §6.2 and §7.2 (`docs/research/2026-10-20-cloud-model-and-pricing-refresh.md`),
FRE-1640 study F5 and F6 (`docs/research/2026-10-21-fre-1640-cloud-role-model-matrix.md`).

## Facts the design rests on (verified 2026-10-21, litellm 1.106.0, offline map)

- `litellm.model_cost["claude-sonnet-5-5"]["supports_forced_tool_use"]` is `False`. Sonnet 5 has no key.
  Haiku 5.5 has `True`. `register_model_pricing` keeps the flag (it calls `litellm.register_model`, which merges).
- litellm raises `UnsupportedParamsError` for a forced choice on a flagged model unless `drop_params` is on.
- litellm 1.106.0 drops `function.strict` on the Anthropic chat path (the built request has no `strict`).
  Strict tool use is therefore not reachable through this client. Structured outputs (`response_format`) are
  out: litellm turns them into a synthetic forced tool on Anthropic (see `session_digest_wire`).
- The span and entailment system prompts already say "Answer only through the tool."
  The digest prompt asks for JSON, and `_reply_payload` already accepts a JSON text reply.

## Design

1. `LiteLLMClient.supports_forced_tool_use` (property): `False` only when litellm's map entry for the
   deployment holds `supports_forced_tool_use: False`. The lookup tries the prefixed model string, then the bare id
   only if that entry's `litellm_provider` equals the client's provider (Codex review item 2).
   An unknown model or a missing key is `True`, which keeps today's request.
2. New module `src/personal_agent/llm_client/forced_tool.py`: `can_force_tool(client: object) -> bool`
   returns `getattr(client, "supports_forced_tool_use", True) is not False`. A client that does not say
   (test fakes, mocks) gets today's request.
3. Each producer picks its choice from its own client, not from the role binding (the eval harness
   overrides the deployment through the client, `get_llm_client_for_key(--model-key)`):
   - `grounding/extractor.py`: `tool_choice=span_tool_choice() if can_force_tool(self._client) else "auto"`.
   - `grounding/entailment.py`: the same with `entailment_tool_choice()`.
   - `second_brain/session_summary.py`: `digest_tool_choice() if can_force_tool(cloud_client) else "auto"`,
     only when the contract is on (off stays `None`).
   The tools list is unchanged (the single tool). The forced dict functions are unchanged.
4. A reply with no tool call uses each producer's existing path:
   - span: empty payload → coverage gaps → non-exempt (fail-closed).
   - entailment: `UNDECIDED` with "returned no verdict".
   - session_summary: text fallback → `parse_model_output` → `SCHEMA_INVALID` for prose, `EMPTY_OUTPUT` for nothing.
     A schema-valid JSON text reply is accepted, as it is today on every deployment
     (`test_text_content_is_still_accepted`). Codex flagged this as not fail-closed. Decision: keep the existing
     path, because the same parser and checks apply and a change would alter reply handling for forced-capable
     deployments. Flagged for master. The measurement counts text replies against tool replies.
5. Not changed: `config/model_roles.yaml` (AC-4), prompts, `drop_params`, the catalog.
   Comments in `config/models.yaml` and the FRE-1631 test docstring that say the producers "must not bind"
   Sonnet 5.5 get one sentence each: the producers now send `auto` there (FRE-1647); a binding is the owner's choice.

## Steps (TDD)

1. Tests first, `tests/personal_agent/llm_client/test_fre1647_forced_tool_capability.py`:
   - property on a real `LiteLLMClient` for `claude_sonnet_5_5` → False, `claude_sonnet` → True,
     `claude_haiku_5_5` → True, a local key → True (offline map).
   - `can_force_tool` on an object without the attribute → True, on `MagicMock()` → True.
2. Producer tests (AC-1, AC-2), in the existing files:
   - `tests/personal_agent/grounding/test_extractor.py`, `test_entailment.py`,
     `tests/personal_agent/second_brain/test_session_summary_contract.py`:
     a fake client with `supports_forced_tool_use = False` → `tool_choice == "auto"`, `tools` is the single tool;
     a fake with `True` and one without the attribute → the full kwargs equal today's kwargs exactly
     (compare the whole dict, not one key).
   - no-tool-call reply under `auto`: span → every classifiable span non-exempt; entailment → `UNDECIDED`;
     session_summary (through `generate_session_digest`) → `FAILED` with `SCHEMA_INVALID`.
3. AC-4: a test that `config/model_roles.yaml` is byte-equal to `origin/main`'s blob is not portable in CI;
   the FRE-1631 `TestNoBinding` tests already assert no role resolves to `claude_sonnet_5_5`. Evidence for AC-4
   is `git diff origin/main...HEAD -- config/model_roles.yaml` empty, posted in the handoff.
4. Implement 1–3 of the design. Run `make test-file` per file, then the gates.
5. Paid measurement (AC-3), after the gates, from this branch, FRE-375 test stack:
   - Preflight posted on the ticket first: environment, a 1-item trace (span) with the request
     `tool_choice: {"type": "auto"}` and the returned `emit_spans` call, the projected calls and cost, and the
     Anthropic credit check (or a statement that the balance cannot be read). Over $8 → ask master.
   - `span_extraction`: `harness.py --samples 3 --model-key claude_sonnet_5_5`, F6 bars.
   - `session_summary`: `session_digest_eval.py`, all default sets, with an in-memory binding driver
     (the FRE-1640 method), arms `claude_sonnet` (same-day control) and `claude_sonnet_5_5`. F5 rule.
   - Report cost per 30 days (FRE-1640 30-day bases) and median latency.

## Commands

- `make test-file FILE=tests/personal_agent/llm_client/test_fre1647_forced_tool_capability.py`
- `make test-file FILE=tests/personal_agent/grounding/test_extractor.py`
- `make test-file FILE=tests/personal_agent/grounding/test_entailment.py`
- `make test-file FILE=tests/personal_agent/second_brain/test_session_summary_contract.py`
- Gates: `make test`, `make mypy`, `make ruff-check`, `make ruff-format`, `pre-commit run --all-files`.

## Risk tier

Standard: `src/` logic on three producers, two on the turn path. Codex plan review required.
Deploy class: `seshat-gateway` rebuild. No binding change, so production requests do not change
(every bound deployment is forced-capable).
