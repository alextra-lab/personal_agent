# Tool-call classification — definitions (fixed before any call is classified, 2026-10-10)

Each row in `enriched_<session>.json` → `calls` is one tool-call attempt by the agent Seshat during a scripted
evaluation session. `turns` gives each turn's user message, whether it was delivered, and the head of the reply.

Row fields: `id`, `turn`, `ts` (UTC), `role` (`primary` = the main model; `sub_agent` = a worker),
`worker` (`researcher` has only `web_search`; `general` has `run_python`, `search_memory`,
`recall_personal_history`), `tool`, `args`, `outcome` (joined log events: exit codes, result counts, errors,
allowlist refusals, loop-gate decisions), `result` (primary only: the tool output as the model saw it, cut to
900 chars), `result_urls` (web_search only).

**Trap:** `outcome.success: true` means only that the tool ran. A `bash` result can still say `exit_code: 1`,
and a `run_python` row can show `sandbox_finished.exit_code` other than 0. Read the result.

## Classes — assign exactly one, in this order of precedence

1. **failed** — the call returned an error or no usable result. Examples: a tool error or exception, a timeout,
   an allowlist refusal (`bash_allowlist_miss` / `approval_denied`), a non-zero exit with no usable output, an
   HTTP error, an empty extraction, zero search results, a validator refusal.
2. **repeated** — the same intent as an earlier call in the same turn, or in an earlier turn whose result is
   still in context, with no new input that justifies it. Examples: the same or near-identical search query,
   re-reading an unchanged file region, re-running an unchanged command, re-listing a directory.
   A deliberate re-run after a change (for example running a script after editing it) is NOT repeated.
3. **workaround** — the call reaches its goal through a detour, because the direct tool, skill or permission is
   missing, refused or unknown to the model. Examples: `bash` + `curl` to Elasticsearch (no telemetry tool),
   a shell heredoc or `python3 -c` to edit a file (there is no edit tool; `write` only overwrites or appends),
   a patch script written to /tmp, `cat`/`sed`/`grep` to read a file when `read` would do, a `web_search` or
   `perplexity_query` to find a URL that the model then guesses, a second command after an allowlist refusal.
4. **useful** — returned what it was meant to return and moved the task forward, and none of the above applies.

## Cause — for every call that is not useful, name one

- `missing_tool` — the right tool does not exist (for example no edit tool, no telemetry tool in stage 3).
- `skill` — a skill is missing, wrong or misleading for this task.
- `instruction` — a tool description or prompt lacks a needed instruction (for example which fields exist).
- `governance` — an approval, allowlist, loop gate or other policy stopped or redirected the call.
- `environment` — the outside world failed: search-engine rate limit, HTTP 403/429, timeout, sandbox limit.
- `guessed_url` — the model fetched or requested a URL it composed itself, and it was wrong.
- `model` — the model's own choice, when a better call was available and documented.

Give a one-line reason per non-useful call. Use `model` only when no other cause fits.
