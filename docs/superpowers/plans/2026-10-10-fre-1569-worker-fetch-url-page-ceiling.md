# FRE-1569 — a worker cannot take a full-size `fetch_url` page

Ticket: FRE-1569 (regression from FRE-1564). Tier: Standard when written; the result is a config and test change. Codex plan review ran.

## Root cause

`fetch_url` returns up to `max_chars` characters. The default is 10,000. The model can ask for
up to 50,000 (`tools/fetch.py`, `_MAX_CHARS_CAP`). FRE-1564 gave the `researcher` worker this
tool. The worker path has no smaller limit, so a worker that asks for 50,000 gets 50,000.

Measured on the two failing traces (ticket): about 19,000 prompt tokens per 50,000-character
page, so about 2.6 characters per token. The worker context is 131,072 tokens. The loop lands
early when the next prompt passes `131,072 - 16,000 = 115,072` (`sub_agent_context_reserve_tokens`).
Six or seven full pages cross that line. That is trace `b791f193`.

The mechanism that fixes this already exists. `SubAgentToolDecision.param_ceilings` (FRE-1473)
bounds a numeric argument for the sub-agent principal only. `dispatch_tool_call` applies it when
`principal == "sub_agent"` and logs `sub_agent_tool_param_clamped`. The primary never reads it.

One path looked like a gap and is not. The clamp skips a non-numeric value, and
`fetch_url_executor` calls `int(max_chars)`, so a string such as `"50000"` seemed to pass.
Codex review found otherwise, and the code confirms it. `ToolExecutionLayer.execute_tool`
validates every argument against the tool's JSON Schema before the executor runs
(`tools/executor.py`, `tools/schema_validator.py`). `max_chars` is type `number`, so a string
fails validation and no page is returned. No `src/` change is needed. A test pins this with a
fake layer that validates the same way.

## Chosen value: `max_chars: 10000`

| Quantity | Value | Source |
|---|---|---|
| Worker context | 131,072 tokens | `config/models.yaml` |
| Usable before landing | 115,072 tokens | context minus 16,000 reserve |
| Start of a researcher turn | about 2,000 tokens | trace `b791f193`, round 1 |
| One page at 10,000 characters | about 3,800 tokens | 2.6 characters per token |
| Seven pages | about 27,000 tokens | the case in the ticket |
| Pages that fit | about 29 | (115,072 − 2,000) / 3,800 |

The ceiling must not sit below the tool default. The clamp acts only on a value the model sent.
If `max_chars` is omitted, the tool applies its own default of 10,000. A ceiling under 10,000
would then not bind. So the ceiling equals the default: a worker never gets more than the page
size the primary gets when it does not ask for more. A test pins `ceiling >= default`.

## Context management (ticket scope item 2)

Finding: no loop change is needed for the failure in the ticket. With the ceiling, seven large
pages use about 27,000 of 115,072 tokens. The test for AC-3 asserts that the estimated prompt
stays under the reserve line with a wide margin.

Not covered by the ceiling: other inputs can still fill a researcher context. `get_library_docs`
returns up to 20,000 tokens a call (`tools/context7.py`). `web_search` allows 50 results, and its
`exa` mode returns full-page text (`tools/web.py`). One round can hold several tool calls. The
Flash-Next run (`948af8ba`) made "many `web_search` and `fetch_url` calls", and how much of it
came from search results is not measured here.

If a live run still stops on `context_reserve` after this change, the next step is to stub old
tool results inside the loop. `_trim_for_landing` does that only at the landing today. That change
alters what the report is written from, and the cache prefix. It needs its own decision, so it is
not in this PR. AC-4 is the test of this finding.

## Steps

1. Test first: governance and dispatch tests, then the config.
   - `tests/personal_agent/governance/test_sub_agent_tools.py`
     - the shipped `fetch_url` decision carries `max_chars` equal to the tool default
     - the shipped config clamps a request for the tool maximum
   - `tests/personal_agent/orchestrator/test_worker_fetch_url_ceiling.py` (new file), real
     config, real `fetch_url_executor`, stub HTTP, a fake tool layer that validates arguments
     against the tool schema as the real layer does
     - AC-1: sub-agent asks for 50,000: the executor receives 10,000 and the event fires
     - AC-2: primary asks for 50,000: the executor receives 50,000 and no event fires
     - AC-3: a researcher run through 7 pages of 50,000 characters ends `completed`
     - seeded negatives: the same calls with the ceiling removed return 50,000, and the
       researcher stops on `context_reserve`
2. Run the new tests. Confirm they fail.
3. `config/governance/tools.yaml`: add `param_ceilings: {max_chars: 10000}` to `fetch_url`, and
   extend its `reason` with the FRE-1569 note.
4. Run the new tests. Confirm they pass.
5. Gates: `make test`, `make mypy`, `make ruff-check`, `make ruff-format`, `pre-commit run --all-files`.

## Acceptance criteria

| AC | Proof | Fails if |
|---|---|---|
| AC-1 worker cannot take a full page | dispatch test: `max_chars` 50,000 from a sub-agent reaches the executor as 10,000; `sub_agent_tool_param_clamped` logged; a numeric-string request returns no page (schema refuses it) | the worker receives more than 10,000 |
| AC-2 primary unchanged | dispatch test: default principal receives 50,000, no clamp event | the primary limit changes |
| AC-3 multi-page researcher finishes | `run_sub_agent` test: 7 pages of 50,000 characters through the real clamp and executor ends `completed`; peak prompt estimate is under the reserve line | it stops on `context_reserve` |
| AC-4 live | master, after deploy: one research turn, a researcher that fetches several pages | any researcher ends `context_reserve` because of page size |

Rules from master: no local model call and no deploy. This plan needs neither.
