# FRE-1597 — the routing run on Flash-Next (ADR-0156 A5, T-D1)

**Ticket:** FRE-1597. **ADR:** ADR-0156, amendment of 2026-10-12 (FRE-1591): AC-F1, AC-F2 and AC-F3 on the
final build, and the routing part of AC-F4. **Builds on:** `scripts/eval/fre1590/triggering.py` (Part 1),
`scripts/eval/fre1594` (the reveal spike, PR #1270) and `scripts/eval/fre1537` (the probe gateway).

## Goal

Measure four things on the FRE-1596 build, on the production primary (llama.cpp, Qwen3.8 Flash-Next):

| Ticket AC | ADR AC | Question |
|---|---|---|
| AC-1 | AC-F4 routing | Does the first call load the right skill of the seven area skills, and no skill on a no-skill prompt? |
| AC-2 | AC-F2 | After `read_skill`, does the revealed tool complete the five-step loop? |
| AC-3 | AC-F1 | Is the first request smaller by about the `query_telemetry` definition? |
| AC-4 | AC-F3 | Does the simple reveal cost at most one costly call per revealed tool, against a control? |

The deliverable is a research note in `docs/research/` with the four tables. The run starts only after the
owner's OK on the preflight numbers. Master provides the eval stack and the llama.cpp window.

## Facts that shape the design

1. **The stub answers text only** (`scripts/eval/fre1537/stub.py`), so the gateway never sends a call 2 today.
2. **Production decorates a `read_skill` result.** Each turn has a source registry (`executor.py:4516`).
   `read_skill` is a typed retrieval tool (`grounding/source_registry.py:336`), so its result gets a
   `_citation` field (`executor.py:8233`, `_with_citation_marker`). An advisory gate decision adds
   `_gate_warning` (`executor.py:8191`). A harness cannot rebuild these offline.
3. **Production places a revealed tool at its registry position** (`registry.py:86-90`), and the offline
   registry lists fewer tools than the gateway (`get_location` and the R2 tools are gated by settings: 16
   offline against 18 or more in a capture).
4. **Production runs every call of a reply** and orders the results (blocked first, then allowed), so a call
   2 after a reply with several calls cannot be rebuilt without running them (Codex, round 1).
5. **The rewritten scenarios do not fit "4 scenarios × 3 draws" for every skill.** `interaction-outcomes` has 3
   scenarios (9 draws). `log-search` and `service-health` have 5 (15 draws). The other four have 4 (12 draws).
6. **The `log-search` scenarios of `evals.yaml` test curl paths past the tool's limits.** None is a task for
   `search_logs`. The four FRE-1594 prompts (`scripts/eval/fre1594/scenarios.yaml`) are tool tasks.
7. **The pre-T-C2 commit is `b02503fc`** (first parent of `1615a50a`). After `1615a50a`, only
   `captains_log/prompt_manifest.py` changed under `src/`, `config/` and `skills/`.
8. **A capture's user message holds the time to the second**, and the memory call id is random.
9. **The captured body carries `cache_prompt: true`.** `cache_prompt: false` skips the prompt cache.
10. **Elasticsearch-eval is not running now** (`docker ps`, 2026-10-13). The probe gateway and the shim need it.

## Owner decisions this plan needs (asked in the preflight, not assumed)

- **D-1, the AC-1 bound for a skill without 4 scenarios.** Proposed: the same share, rounded up:
  `first >= ceil(draws × 10 / 12)`, so 8 of 9, 10 of 12 and 13 of 15. Until the owner decides, the report
  shows the count and both readings.
- **D-2, the AC-2 prompts.** Proposed: the four FRE-1594 prompts, on the final build.
- **D-3, the second step on skills that reveal a tool.** Each such call 2 changes `tools`, so llama.cpp
  re-processes the whole prompt (FRE-1594 F4: median 31.4 s). Proposed: run it, as scope item 1 says.
- **D-4, the cloud primary.** ADR AC-F2 measures the cloud primary too, with no bound. The ticket scopes the
  run to Flash-Next. A cloud run is a paid call during the model-testing hold. Proposed: skip it, because
  T-B measured it (simple form 12 of 12) and the bound applies to the local primary only. `loop run
  --provider cloud` supports it if the owner wants it (FRE-1594 cost: about $3 for one form).

## Design

### 1. The stub can script a tool call (`scripts/eval/fre1537/stub.py`, additive)

If `<out>/next_replies.json` holds a non-empty JSON list, a request that carries `tools` takes the first entry
and the stub writes the rest back. An entry is `{"tool_calls": [{"name": ..., "arguments": {...}}]}` or
`{"content": "..."}`. A tool-call entry is streamed as OpenAI `tool_calls` deltas with `finish_reason`
`tool_calls` (and as a `message.tool_calls` reply when not streamed). With no file or an empty list, the stub
does what it does today. FRE-1537 and FRE-1590 never write the file, so they are unchanged.

### 2. A capture of two calls (`scripts/eval/fre1597/calls.py`)

For a scenario item, the capture scripts `[read_skill(expected), "OK"]`. The gateway runs the real
`read_skill`, applies the real reveal (`sessions.revealed_tools` on the eval Postgres) and sends call 2 to the
stub. So call 2 is the production body, with the `_citation` field, the tool placement and anything else the
executor adds.

- `primary_calls(call_files, prompt)`: the requests that carry `tools` and whose last user text holds the
  prompt, in stub order.
- `TwoCalls.from_capture(first, second)` checks the capture, else `FidelityError`:
  - `second.messages == first.messages + [assistant, tool]`, where the assistant message has exactly one
    `read_skill` call of the scripted skill, and the tool message answers it;
  - every key other than `messages` and `tools` is equal;
  - the tools of `second` are the tools of `first` plus the revealed tools of the skill
    (`orchestrator.skills.tools_revealed_by`), in the order that `second` holds them.
  It keeps `skill_content` (the tool message content) and `second_tools`.
- `TwoCalls.second_request(reply)`: `first.messages`, then the assistant message of the model's real reply
  (`fre1594.reveal.assistant_message`, turn 0, the production id rule), then the captured tool message with
  its `tool_call_id` set to the rewritten id of the model's `read_skill` call. The other keys and `tools` are
  `second`'s. A test checks that the id rule reproduces the captured stub call's id.

### 3. Part 1 with the second step (`scripts/eval/fre1597/routing.py`, AC-1)

Items: every `evals.yaml` scenario whose expected skill is one of the seven A4 skills (29), and the 20
no-skill prompts (`load_items()` filtered).

- `capture`: one turn per item. A scenario item scripts its expected skill. A no-skill item scripts text
  only. It saves both calls and the gateway's `/health` build fingerprint.
- `replay`: a warm-up call (the first item, `max_tokens` 1, its own nonce), then, per draw and per item:
  1. **Call 1:** the captured first body through `fre1594.transport.LlamaTransport` (streamed; the
     production sanitiser and adapter). The row keeps the Part 1 fields (`tool_names`, `read_skill_names`,
     `read_skill_malformed`, `finish_reason`, `usage`), `timings` and `request_sha256`.
  2. **Call 2:** only when call 1 has exactly one tool call and it is `read_skill` of the expected skill.
     Else the row records `second.skipped` with the reason. Call 2 records the tool names, the revealed tools,
     `called_revealed`, `finish_reason`, `timings`, `ok`, `error` and `request_sha256`. No tool runs.
- `score`:
  - **`first`** (the AC-1 count): the first tool call of call 1 is `read_skill` of the expected skill, and no
    other skill loads in call 1. The Part 1 `right` count (`triggering.classify`) is reported beside it.
  - **Area confusion:** a `read_skill` of one area skill in call 1 of a scenario that expects another area
    skill. The bound is 0. A load of a skill outside the area is reported, not bounded.
  - **No-skill:** any `read_skill` call, malformed ones included, in 60 draws. The bound is 0.
  - **Second step table:** for each skill, the draws with call 2, `called_revealed`, and the first tools.
  - Every row must show the same `system_fingerprint` (the llama.cpp build), and every item must have all
    draws, or the verdict is `incomplete`.
- Fingerprint (`fre1594.spike.ensure_fingerprint`, every key pinned): the model, the quant, the llama.cpp
  build, the chat-template hash (`--props-file`), the server arguments, the sampling keys, the system-prompt
  and tools hashes, each item id with the hash of its text, the gateway build fingerprint, the draws and the
  warm-up.

### 4. The full tool loop and the cache cost (`scripts/eval/fre1597/loop.py`, AC-2, AC-4)

The FRE-1594 session in the simple form (`fre1594.session.run_draw`), over the four FRE-1594 prompts captured
on the final build with `[read_skill(skill), "OK"]`.

- `fre1594.session.Draw` gets two optional fields, default `None` (done, tested): `skill_content` (the
  `read_skill` result of both arms) and `place_tools` (the treatment's tools after the reveal). The loop sets
  them from `TwoCalls`: the captured skill content, and the captured call-2 tools. The control keeps the call-1
  tools and the same skill content, so it differs only by the reveal (AC-F3).
- Call 3 carries the shim's tool result. Production adds no `_citation` field to a `search_logs` or
  `measure_model_latency` result (`grounding/source_registry.py:328`), so this is the production shape.
- The `_citation` value in the captured `read_skill` result depends on the capture turn's trace id. The
  replay sends it as opaque prompt text, as the same turn would have sent it.
- Score: `fre1594.score.loop_count` (AC-2: held >= 10 of 12, all 12 done) and
  `fre1594.score.cache_verdict(rows, "simple")`. AC-4 fails on a `fail` verdict, on any lineage-void pair and
  on missing timings.

### 5. The prompt-size drop (`scripts/eval/fre1597/prompt_size.py`, AC-3)

- `capture --label B|A --chat-url ...`: one turn of one fixed no-skill prompt with a text reply. The command
  always runs from this worktree. For `B`, the gateway runs from a temporary worktree at `b02503fc`, and
  `capture` records its `/health` build fingerprint.
- `measure`:
  1. `B-minus` = `B` with the one `query_telemetry` entry removed from `tools`.
  2. `A` is aligned to `B`: its non-system messages become `B`'s. It refuses unless the prompt text is the same
     and the message lists differ only in the time line and the memory call id.
  3. Each body is sent once, B, then B-minus, then A, with `cache_prompt: false` and `max_tokens: 1`. A reply
     with `cache_n` other than 0 is refused. The hash of each sent body is recorded.
  4. The report: the three counts and hashes, `tokens(B) > tokens(B-minus)`, the ratio
     `(B - A) / (B - B-minus)` against 0.8, and a diff of `B` against `A`.
  5. **The diff has a verdict.** Each changed block of system-prompt lines and of tool-definition lines must
     name a skill or tool that T-C2 removed, renamed, split or added. The only removed tool is
     `query_telemetry`, no tool is added, the kept tools keep their order, and every other key is equal.
     Any other difference is listed as `unexpected`, and AC-3 fails.

## Steps (TDD, offline tests with fakes only)

| # | Step | Test |
|---|---|---|
| 1 | `fre1594.session.Draw` hooks | done: `tests/scripts/test_fre1594_reveal_spike.py` 52 passed |
| 2 | stub `next_replies.json` | `tests/scripts/test_fre1537_stub.py` (new or extended): a scripted tool call streams as `tool_calls` and the production adapter reads it; the list is consumed; no file keeps today's reply |
| 3 | `calls.py` | `tests/test_eval/test_fre1597_calls.py`: a good two-call capture splits; an extra message, a changed key, a second tool call or an unexpected tool set is refused; `second_request` swaps the assistant message and the call id |
| 4 | `routing.py` | `tests/test_eval/test_fre1597_routing.py`: a fake transport; call 2 only after exactly one `read_skill(expected)`; the `first` rule (a `bash` before the `read_skill` fails); area confusion; a no-skill load; an incomplete item |
| 5 | `loop.py` | `tests/test_eval/test_fre1597_loop.py`: AC-2 and AC-4 from fixed rows; a lineage-void pair fails AC-4 |
| 6 | `prompt_size.py` | `tests/test_eval/test_fre1597_prompt_size.py`: B-minus removes one tool only; the alignment refuses a different prompt; `cache_n` > 0 is refused; the 80% rule at the boundary |
| 7 | `README.md`, the preflight | — |

Commands: `uv run pytest -q tests/test_eval/test_fre1597_*.py tests/scripts/test_fre1594_reveal_spike.py
tests/test_eval/test_fre1590_triggering.py`, then the Step-6 gates. No `make test` while a llama.cpp window is
open.

## The run (after the owner's OK)

1. Master starts the eval stack (Elasticsearch-eval included) and opens the llama.cpp window. All seats hold
   `make test`, `tests/test_eval/` and evals.
2. **B:** `git worktree add <scratch>/pre-tc2 b02503fc`, `uv run python -m scripts.eval.fre1537.gateway up`
   from there, then from this worktree `prompt_size capture --label B`, then `gateway down`, then remove the
   worktree.
3. **A and the items:** `gateway up` from this worktree, `prompt_size capture --label A`, `routing capture`,
   `loop capture`, `gateway down`.
4. **Preflight (no model call):** item and token counts, every capture's fidelity check, the call-1 tools
   against production's latest `tools_passed_to_llm`, the time projection, the shim smoke. Put it before the
   owner with D-1 to D-3.
5. After the OK: a dry run (`routing replay --limit 2 --draws 1`, `loop run --limit 1 --draws 1`). Read rows.
6. `prompt_size measure`, `routing replay`, `loop run`, then each `score`.
7. The research note, then the PR.

**Projection (to refine in the preflight):** call 1 about 147 × 6 s = 15 min; call 2 about 66 × 40 s + 21 × 15 s
= 50 min at most; the loop about 30 min (FRE-1594 simple: 28 min); AC-3 about 3 min. Total about 1 h 40 min.
Local only, no paid call.

## Risks

- **The call-2 transcript after a multi-call reply.** The routing run skips call 2 there (reported). The loop
  answers other calls with `not_run`, as FRE-1594 did.
- **Gitignored build inputs in the B worktree.** The freshness fingerprint hashes untracked files too
  (`gateway_freshness.py:60`). `capture` records each gateway's fingerprint, and the AC-3 diff shows any
  difference outside T-C2.
- **A co-tenant on llama.cpp.** Another seat's test or eval cut FRE-1594 dry draw 2. Master holds every seat.

## Codex review

- **Round 1 (2026-10-13), on the first plan:** call 2 rebuilt offline differs from production after a
  multi-call reply (CONFIRMED) → call 2 only after exactly one `read_skill`. The offline-registry fidelity
  check was too weak (RISK) → call 2 now comes from the gateway, and the citation marker found in checking it
  confirms the change. `classify` ignores the order of calls (CONFIRMED) → the `first` rule. Area confusion
  must be limited to the area → done. The 10-of-12 share is an owner decision → D-1. AC-4 must fail on a
  void pair → done. B must be captured with this worktree's command and its fingerprint recorded → done.
- **Round 2 (2026-10-13), on this plan:** the stub-scripted call makes the gateway send exactly one more
  primary request, and nothing is inserted between rounds in the ordinary path (OK). The swapped assistant
  message and the round-0 ids are faithful (OK). The `_citation` value is turn-specific but valid to replay
  (RISK, documented). The plan wrongly said that production adds `_citation` to a `search_logs` result →
  corrected. AC-F2 asks for the cloud primary (CONFIRMED) → D-4. AC-3 must fail on a difference outside T-C2
  (RISK) → the diff verdict in step 5.
