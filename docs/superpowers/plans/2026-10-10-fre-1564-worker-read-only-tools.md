# FRE-1564 — Worker read-only tools, split by direction, and a native read-only telemetry tool

Ticket: FRE-1564 (Approved, High). Backing ADR: ADR-0150 D2 (worker registry), ADR-0154 D7 (planner
qualification), ADR-0028 (tool tiers). Owner decisions of 2026-10-10: option B (all read-only tools
reach workers), "1 a" (a native read-only Elasticsearch tool for `general`), "2 yes" (`fetch_url` to
`researcher`, overriding the FRE-1463 refusal while FRE-1360 is open).

Risk class: **Standard/Complex** (new `src/` tool, governance config, planner prompt). Codex plan
review required. Diff class: **escalated** (a new data-access path and a reversed refusal). The PR
flags the owner `/code-review ultra`.

## Facts the plan rests on (each read at origin/main 85ea515a)

1. The worker grant reads only `sub_agent_tools` in `config/governance/tools.yaml`
   (`governance/sub_agent_tools.py:evaluate_sub_agent_tool_grant`). It never reads a tool's
   `allowed_in_modes`. A tool with no entry is denied. ALERT and DEGRADED deny every worker tool in code.
2. A worker receives a tool only if the shared registry holds it (`sub_agent._build_tool_defs`).
   The four `mcp_*` Elasticsearch entries are fossils: the cloud gateway starts only
   `sequentialthinking` and `context7`. `mcp_search` is DuckDuckGo (outbound). `essential_health_check`
   is a category, not a tool. So no worker can read telemetry today.
3. `run_python` takes a `network` argument. `True` attaches the sandbox to `seshat_cloud-sim`, the
   network that holds Elasticsearch, Postgres and Neo4j and has outbound access. Nothing in the
   worker path blocks it (`tool_dispatch.dispatch_tool_call` clamps numbers only). So `general` has a
   way out today, and after this change it would hold private reads too.
4. `agent-logs-*` holds conversation-derived fields: `user_message`, `message_preview`,
   `messages_preview`, `query_preview`, `query_text`, `raw_preview`, `context_messages`, `arguments`,
   `results`, `sources`, `query`, `query_params`, `user_id`, `response_headers` (checked on the live
   mapping, 2026-10-10, 840 fields). Elasticsearch has no per-user scoping.
5. Time fields differ by family: `agent-logs` and `agent-topology` use `@timestamp`;
   `agent-monitors-slm-health` uses `probed_at`.
6. The planner prompt renders each type's description and its granted tools
   (`expansion_controller._build_planner_system_prompt`), so tool names and descriptions are planner
   input. The planner request changes, so ADR-0154 D7 must pass again.

## Design decisions

### D-A. The tool: `query_telemetry` (Tier 1, `tools/telemetry_query.py`)

The model never supplies a URL, path, header or request body. The tool builds the request.
**Codex review (High): a denylist over Elasticsearch DSL cannot be a boundary** (`terms_set` with
`minimum_should_match_script`, `global` aggs that ignore the time filter, `inner_hits`, `top_metrics`,
field aliases). The tool therefore validates the query and aggregations against an **allowlist of
shapes and of field names**. Anything not listed is refused, so a new operator or a new logged
field is unreachable until a person adds it.

| Parameter | Rule |
|---|---|
| `action` | `search` or `count`. |
| `index` | A family from a fixed map to its time field: `agent-logs` (`@timestamp`), `agent-topology` (`@timestamp`), `agent-monitors-slm-health` (`probed_at`). The tool builds `/{family}-*/_search` or `/_count`. |
| `query` | Optional object, validated (below). Default `match_all`. Wrapped as `bool.filter[range(time_field >= now-<since>)]` + `must[query]`. |
| `aggs` | Optional object, `search` only, validated (below). |
| `fields` | Optional list of allowed field names. Becomes `_source.includes`. Default: the family's full allowlist, never "all". |
| `size` | Default 20, cap 50, floor 0. |
| `since` | `^\d{1,3}[mhd]$`, at most 30 days. Default `24h`. |

**Query allowlist.** A node is a dict with one key. Operators: `match_all`; `bool` (keys `must`,
`filter`, `should`, `must_not`, `minimum_should_match`; children are nodes); `term`, `terms`,
`match`, `match_phrase`, `range`, `prefix`, `exists`. For the field-keyed operators the one inner key
is a field and must be allowed. Values are scalars (string at most 200 characters, number, bool); a
`terms` value is a list of at most 100 scalars; `range` allows `gte, gt, lte, lt, format, time_zone`;
`match`/`term`/`prefix` also allow `value` or `query`. `exists` takes `{field: <allowed>}`. Nesting at
most 8 deep.

**Aggregation allowlist.** Each named agg has one type plus an optional `aggs` (depth at most 4, at
most 10 aggs in all). Types and their allowed keys: `terms` (`field`, `size` at most 100, `order`,
`min_doc_count`), `date_histogram` (`field`, `calendar_interval` or `fixed_interval` matching
`^\d{1,3}(ms|s|m|h|d)$` or a calendar word, `min_doc_count`), `avg|sum|min|max|value_count|
cardinality|stats` (`field`), `percentiles` (`field`, `percents`: at most 10 numbers). No `global`,
`top_hits`, `top_metrics`, `scripted_metric`, no `script`, no `missing`.

**Field allowlist (positive, per family, one constant).** A field is allowed if it is listed, or is
a listed field plus `.keyword`. `agent-logs`: `@timestamp, level, event_type, message, error,
trace_id, session_id, action, task_type, mode, tool_name, model, model_id, role, provider,
input_tokens, output_tokens, cache_read_tokens, cost_usd, elapsed_ms, duration_ms, latency_ms,
response_time_ms, success, turn_count, decision, status, component, backend, reachable, count,
http_status, status_code` (all verified on the live mapping 2026-10-10 except `response_time_ms`,
`component`, `backend`, `status` checked in step 2). `agent-topology`: `@timestamp, session_id,
trace_id, task_id, task_type, topology, role, model_role, complexity, decomposition_strategy,
decomposition_reason, planner_decision, planner_mode, planner_failure_reason, planner_gate_reason,
planner_duration_ms, planner_prompt_tokens, planner_completion_tokens, input_tokens, output_tokens,
first_token_ms, latency_total_ms, authoritative_cost_usd, result_type, gateway_label,
intent_confidence`. `agent-monitors-slm-health`: every field of the live mapping (none holds
text). A refusal for a field names the family's allowed fields, so the list also acts as the
schema the model needs.

Because every field is allowlisted, the denied conversation-content fields of `agent-logs`
(`user_message, message_preview, messages_preview, query_preview, query_text, raw_preview,
context_messages, arguments, results, sources, query, query_params, user_id, response_headers`) are
unreachable by construction, and so is any content field logged later. A test asserts none of them
is in the allowlist.

One `POST` per call, `timeout` 10 s in the body and 15 s in the client, built by
`create_guarded_http_client`, base URL `settings.elasticsearch_url`. The result is trimmed to
20,000 characters with `truncated: true`. Every refusal is a `ToolExecutionError("refused: <reason>")`
raised before any request.

**Conversation content.** `message` and `error` are free text written by the agent's own logging.
They can carry a fragment of user input in an exception string. This is the residual risk, stated in
the PR and the ADR note. No family that holds conversation text is allowed: `agent-captains-*`,
`agent-insights-*`, `user-turn-ratings-*`, `caddy-access-*` are out. A future index alias that matches
`agent-logs-*` would resolve into the tool; the plan notes it and relies on the repo's fixed index
naming (no aliases today).

**Grounding (Codex Medium).** `query_telemetry` is not added to `TYPED_RETRIEVAL_TOOLS` in
`grounding/source_registry.py`. Telemetry is partly agent-written, so it must not become a citable
source by accident. Default-deny (`UNCLASSIFIED_TOOL`) matches today's `bash` + `curl` route, which
yields no citable source either. A test pins this outcome. The decision goes in the ADR note.

Registration: `tools/__init__.py` registers it unconditionally (Elasticsearch is core substrate).
**Worker-only (build-time decision):** the primary lists every registered tool in its tool array
and in its tool-awareness prompt, and the tool costs about 535 tokens per turn. The primary reads
telemetry with `bash` and `curl` and the skill docs tell it to. So `ToolDefinition.worker_only`
hides the tool from every listing the primary reads (`ToolRegistry.list_tools` and
`get_tool_definitions_for_llm` default `include_worker_only=False`), and only
`sub_agent._build_tool_defs` opts in. The ticket allowed either; this keeps the primary unchanged.
Governance `tools:` entry: category `read_only`, modes NORMAL/ALERT/DEGRADED/RECOVERY, risk low, no
approval, timeout 20, rate limit 120/hour.

### D-B. Worker lists

| Type | Tools |
|---|---|
| `researcher` (outward) | `web_search`, `fetch_url`, `get_library_docs` |
| `general` (inward) | `run_python`, `search_memory`, `recall_personal_history`, `query_telemetry`, `notes_search`, `read_skill` |

`sub_agent_tools` entries (each with its own reason): grant `get_library_docs`, `read_skill`,
`notes_search`, `query_telemetry`. Flip `fetch_url` to `granted: true` with a reason that cites the
owner's decision of 2026-10-10 and FRE-1360, and keeps the old refusal's text as history.
`notes_search` registers only when R2 is configured. That condition stays. `worker_types.py` lists the
tool for `general` regardless; a missing registration means the tool is simply absent from
`_build_tool_defs` (the existing behaviour for any unregistered name).

### D-C. Close the `run_python` network route for workers

Add `param_forced: dict[str, bool]` to `SubAgentToolDecision`. `clamp_sub_agent_tool_params` sets each
forced parameter for the sub-agent principal and records a clamp (existing `ParamClamp` shape) when
the model supplied a different value. `run_python` gets `param_forced: {network: false}`. The primary
never calls this path. This is the smallest change that makes the `general` inward/no-outbound claim
true, and it is a fold-in under the ticket's own "close it or propose how".

### D-D. Descriptions (rendered into the planner prompt)

- `general`: "Answers a bounded question from its own knowledge, a computation, the user's own
  memory or notes, or the system's own logs, metrics, errors and health, and reports in text"
- `researcher`: "Finds facts on the open web, reads a known web page or library documentation, for
  one bounded question and reports them as data with sources and gaps"

### D-E. Rule test (AC-3)

A registry test with two constants, `OUTBOUND = {web_search, fetch_url, get_library_docs}` and
`PRIVATE = {search_memory, recall_personal_history, notes_search, query_telemetry}`, asserts no type
holds both. A seeded-negative test builds a copy of the registry with `fetch_url` added to `general`
and asserts the rule function reports the violation. `run_python` is classed by D-C: the test also
asserts the shipped `run_python` decision forces `network` false.

## Steps (TDD: each test first, confirm it fails, then implement)

1. **Governance forced params.** Tests: `tests/personal_agent/governance/test_sub_agent_tools.py` —
   a forced param overrides a supplied `network: true`, records a clamp, sets an omitted or
   wrong-typed value silently, leaves other params. `tests/personal_agent/orchestrator/test_tool_dispatch.py`
   (beside line 146) — at the dispatch boundary, `run_python` from `principal="sub_agent"` reaches the
   executor with `network` false, and `principal="primary"` is unchanged. Implement in
   `governance/models.py` and `governance/sub_agent_tools.py`. Run:
   `uv run pytest tests/personal_agent/governance/test_sub_agent_tools.py tests/personal_agent/orchestrator/test_tool_dispatch.py -q`.
2. **The tool.** Test file `tests/personal_agent/tools/test_telemetry_query.py` (AC-1), using
   `httpx.MockTransport` through a patched client factory so a refused call is proven to send no
   request (`transport.calls == 0`). Cases: search and count on each allowed family work and build
   the expected URL and body (range filter on the family's time field); write verbs, a raw `path`,
   another index (`caddy-access`, `agent-captains-captures`), wildcard family tricks (`agent-logs-*,x`),
   each denied key, each denied field in query/aggs/fields, depth and size bounds, `since` bounds,
   size cap 50, forced `_source.excludes`, timeout, ES 4xx mapped to a short error, truncation.
   Add the Codex bypass inputs as tests: `terms_set` with a script, `global` agg, `inner_hits`,
   `top_metrics`, `top_hits`, a terms lookup, an alias-style unknown field, `{"term":{"event_type":
   "user_message"}}` (a harmless value, allowed), `user_message.keyword` (refused). Implement
   `tools/telemetry_query.py`, register in `tools/__init__.py`. Before the allowlist is final, check
   the four unverified field names on the live mapping (read-only `GET`). Run:
   `uv run pytest tests/personal_agent/tools/test_telemetry_query.py -q`.
3. **Governance config.** Edit `config/governance/tools.yaml`: `tools.query_telemetry`; four new
   `sub_agent_tools` entries; `fetch_url` flip; `run_python` `param_forced`. Update the tests that
   assert `fetch_url` is refused or that the decision set is `{True, False}`:
   `tests/test_config/test_governance_loader.py:234`,
   `tests/personal_agent/governance/test_sub_agent_tools.py:297`,
   `tests/personal_agent/orchestrator/test_expansion_controller.py:282`. Add a grounding test that
   `query_telemetry` is `UNCLASSIFIED_TOOL`. Run:
   `uv run pytest tests/test_config/test_governance_loader.py tests/personal_agent/governance tests/personal_agent/orchestrator/test_expansion_controller.py tests/personal_agent/grounding -q`.
4. **Worker lists and descriptions.** Edit `orchestrator/worker_types.py`. Update
   `tests/personal_agent/orchestrator/test_worker_types.py` (the "two types / union of tools" tests).
   New `tests/personal_agent/orchestrator/test_worker_tool_split.py`:
   AC-2 (each type's granted list after governance holds each added tool, no side-effecting tool —
   side-effecting set: `bash, write, read, notes_write, artifact_*, create_linear_*, find_linear_issues,
   list_linear_projects, get_location, perplexity_query, expand_tool_result`), AC-3 (the rule and
   its seeded negative), AC-4 unit (the rendered planner prompt contains both new descriptions and
   `query_telemetry` under `general`, and `fetch_url` under `researcher`). The grant is computed with
   the real `load_governance_config()` and `evaluate_sub_agent_tool_grant` in NORMAL mode. Run:
   `uv run pytest tests/personal_agent/orchestrator/test_worker_types.py tests/personal_agent/orchestrator/test_worker_tool_split.py tests/personal_agent/orchestrator/test_expansion_controller.py -q`.
5. **ADR-0150 D2 amendment** — one dated section appended to
   `docs/architecture_decisions/ADR-0150-the-worker-returns-data-not-prose.md`: decisions, lists, the
   rule, the reason (FRE-1517 stage 3 F4), the `run_python` closure, the deny-list residual risk.
6. **AC-7 cost.** A script `scripts/eval/fre1564/prompt_cost.py` renders (a) the planner system prompt
   before and after, and (b) each worker type's tool definitions, using the registry; token counts
   come from `llm_client.token_counter.estimate_tokens` (an estimate, stated as such) and also the
   served model's tokenizer count if the llama.cpp server answers. Output pasted in the PR.
   Run: `uv run python -m scripts.eval.fre1564.prompt_cost`.
7. **Gates:** `make test`, `make mypy`, `make ruff-check`, `make ruff-format`,
   `pre-commit run --all-files`. Commit. Run `feature-dev:code-reviewer` and `security-review` on
   `git diff origin/main...HEAD`. Fix confirmed findings.
8. **Local-model steps — gated by master.** Tell master before each, then run:
   - AC-4 probe: three telemetry questions to the planner on `qwen3.8-flash-next` through
     `scripts/eval/fre1498/planner_probe.py` (add a three-fixture file and the new granted-tool list to
     the probe's constant). Record the plans.
   - AC-5 D7: the `scripts/eval/fre1537` steps 1–7 with `--mode thinking_off`; post `report.txt` and the
     fingerprint. 11 of 11 required. Fewer: the change does not ship.
9. PR, then the Linear handoff comment to master (per-criterion evidence, self-review, runbook, fold-ins).

## Acceptance-criteria map

| AC | Proof |
|---|---|
| AC-1 read-only tool | `test_telemetry_query.py`, with `calls == 0` on every refusal, including the Codex bypass inputs |
| AC-2 tools reach workers | `test_worker_tool_split.py::granted` |
| AC-3 no outbound + private | `test_worker_tool_split.py::rule` and seeded negative |
| AC-4 planner routes telemetry | prompt unit test + recorded probe (step 8) |
| AC-5 D7 11/11 | step 8 report with fingerprint |
| AC-6 live | master or explore after deploy (post-deploy runbook in the handoff) |
| AC-7 cost | step 6 output in the PR |

## Out of scope / not done

- No write tool for any worker. No change to the primary's `bash` route.
- `agent-captains-*`, `agent-insights-*`, ratings and caddy indices stay unreachable by the tool.
- No MCP gateway change. The four fossil `mcp_*` entries stay as they are.
- Post-deploy AC-6 and the deploy are master's.
