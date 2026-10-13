# FRE-1603 — one owner for the monitor indices, trigger words first, shorter catalog lines

**Ticket:** FRE-1603 (ADR-0156 A5). **Source:** FRE-1597 note `docs/research/2026-10-13-fre-1597-routing-run.md`,
F1, F3 and Proposals 1–3. **Owner decision:** 2026-10-13, "Yes" on the three proposals, 80% target kept.
**Class:** Standard (skill text ships in the gateway image and drives routing). **Deploy:** `seshat-gateway` rebuild.

## Scope

Master corrected scope item 1 and AC-1 on the ticket at about 14:45Z, after the first build. The first text gave
every monitor index to `log-search`. That contradicts the unchanged scenario `service-health#4`. The owner of each
index is now chosen by objective.

| # | Change | Files |
|---|--------|-------|
| 1 | `service-health` owns `agent-monitors-slm-health` only and keeps its `search_logs` read. `log-search` owns the five other families. Each "Not for…" line names the other skill for the other part | `skills/service-health/SKILL.md`, `skills/log-search/SKILL.md` |
| 2 | The `log-search` description puts its trigger words first: log events, counts, an index listing, ES\|QL | `skills/log-search/SKILL.md` |
| 3 | The A4 descriptions are at least 10 tokens shorter in total. Each keeps its objective and its "Not for…" clause | `skills/{log-search,service-health,model-latency}/SKILL.md` (the first build also shortened `spend-and-tokens` and `turn-explanation`) |
| 4 | Re-run AC-1 routing and AC-3 prompt size with the FRE-1597 harness and pins, in a window from master, after the owner OKs the preflight | `scripts/eval/fre1597/*` (no change), run directory |

No tool, no tool definition and no `provides-tools` line changes. `interaction-outcomes` and `graph-inspection`
descriptions are not touched: `interaction-outcomes` passed with no slack (8 of 9).

## Assumptions and risks

1. **Three skills sit at their bound** (`log-search` 13 of 15, `service-health` 13 of 15, `interaction-outcomes`
   8 of 9) and temperature is 1.0. A miss on the re-run may come from noise. The report states the draws.
2. **`log-search` lists `agent-monitors-slm-health` as one of the tool's three families** (a fact about the tool and
   a row of its family table). The row says that `service-health` reads it. Its rules, examples and interval text do
   not name it.
3. **Characters stand in for tokens until the re-run.** FRE-1597 measured 1,340 characters = 298 tokens, 4.5 characters
   per token. The bound is 10 tokens, about 45 characters. The final text saves 88 characters, about 20 tokens.
   The `log-search` "Not for…" clause names a third skill, so it grew. It is now in the compact form
   "(the `service-health` skill)".
4. **YAML.** A description is an unquoted scalar, so it cannot hold `: `. The first draft of the `log-search` line did,
   and 22 validator cases failed. The line uses " - " as the other lines do.
5. **Superseded by the correction:** the first plan expected `service-health#4` to route away and proposed dropping
   `provides-tools: search_logs`. With the split, `service-health` still reads the index with the tool, so no ADR
   amendment is needed.

## Steps

Every step is one commit unless it says otherwise. Run all commands from this worktree.

### 1. Failing tests first

`tests/docs/test_monitor_index_owner.py` (17 cases):

- `service-health` description, before "Not for": holds "monitor" and "local model server", no other family, no
  "pipeline". (AC-1)
- `service-health` "Not for…" line holds "monitor" and `` `log-search` ``. (AC-1)
- `service-health` body: the only `agent-monitors-*` name is `agent-monitors-slm-health`, with the `search_logs` call
  and `probed_at`. It holds a stale rule. (AC-1)
- `log-search` description: claims "monitor indices", and its "Not for…" line names `` `service-health` `` and "model
  server". (AC-1)
- Each of the five other families is in the `log-search` body and in no part of `service-health`. (AC-1, one case each)
- No `log-search` line pairs `agent-monitors-slm-health` with a `search_logs(` call or an interval. (AC-1)
- `log-search` stale rule: a line with "stale" and "interval", and one that gives `cache-reset-cadence` "no interval".
- `log-search` first sentence holds "log events", "counts", "indices" and "ES|QL", one case each. (item 2)
- The seven A4 descriptions total at most `3738 - 80` characters. 80 characters is about 18 tokens at 4.5 characters
  per token, against the 10 tokens that AC-3 needs. (item 3)

Run: `uv run pytest tests/docs/test_monitor_index_owner.py -q`. Result before the second round of edits, observed:
5 failed, 12 passed. The 12 that pass are guards.

### 2. Edit the skills

- `skills/service-health/SKILL.md`:
  - description: claims the Postgres, Elasticsearch, Neo4j, Redis and Tempo probes and the health monitor of the local
    model server. Its "Not for…" line names `log-search` for the other monitor indices.
  - body: Rules name the one monitor and point the others to `log-search`. "Step 3 — The monitor of the local model
    server" holds the `search_logs` call and `probed_at`. Success holds the stale rule (5 minutes by default).
    Recovery holds the "no document" row.
- `skills/log-search/SKILL.md`:
  - description: trigger words first. The "Not for…" line names `service-health` for the model-server monitor.
  - body: Rules hold the scheduled-monitor rule and one line that gives the model-server monitor to `service-health`.
    Success names the three scheduled monitors (1 hour, 1 hour, 1 day) and the two event-driven ones.
- `skills/{model-latency,spend-and-tokens,turn-explanation}/SKILL.md`: drop redundant phrases in the description
  only. No trigger word and no "Not for…" clause is removed.

Run: `uv run pytest tests/docs/test_monitor_index_owner.py -q`. Expected: 17 passed.

### 3. Regression gates on the real inventory

```bash
uv run pytest tests/docs/test_skill_validator.py tests/docs/test_skill_conversion.py tests/docs/test_skill_scripts.py \
  tests/personal_agent/orchestrator/test_skill_naming_rules.py \
  tests/personal_agent/orchestrator/test_telemetry_tools_visibility.py \
  tests/personal_agent/governance/test_skill_script_rules.py -q
```

Expected: all pass. This covers the A3 `not_for` validator on the real inventory (ticket AC-1).

### 4. Prompt corpus

`docs/reference/PROMPT_CORPUS.md` is generated (`make render-prompt-corpus`). No test or hook pins it, so a skipped
step passes every gate and leaves the old "Step 3 — The monitor indices" in the reference. Regenerate it and commit
it alone. The header embeds the HEAD SHA and commit time, so the diff holds the header too. The file was at `6e2a36d3`,
so the diff also holds `interaction-outcomes` text that changed before this ticket. The renderer needs the
`cl100k_base` tiktoken cache, and it loads it from the network when the cache is absent.

### 5. Gates and self-review

`make test` · `make mypy` · `make ruff-check` · `make ruff-format` · `pre-commit run --all-files`. Commit first. Then
run `feature-dev:code-reviewer` on `git diff origin/main...HEAD`. `security-review` is skipped: no input, subprocess,
file, auth, secret or network code changes.

### 6. PR, then the re-run request

Open the PR. Post the handoff comment to master. It holds the preflight request: the owner's OK, a llama.cpp window,
the `git rev-parse` of the final build, and the two risks above. The re-run follows `scripts/eval/fre1597/README.md`
steps 1–15 with `loop` skipped (AC-2 and AC-4 of FRE-1597 pass and are not re-run). Only `B` and `A` are captured.
`B-minus` is built from `B` in memory. `prompt_size measure` has no fingerprint check, so a reused `B` from
`telemetry/evaluation/fre1597-routing-run/20261013-1215/` needs a manual pin check (build `da413fad…`, system prompt
`8c15b750…`, tools `a8b49903…`, `.env` values). If one differs, capture `B` again at `b02503fc`.

## Acceptance criteria

| Ticket AC | Proof | When |
|-----------|-------|------|
| AC-1 one owner | `test_monitor_index_owner.py` and `test_skill_validator.py::*:not_for` | this PR |
| AC-2 routing passes | `routing score` on the new run directory | re-run window |
| AC-3 prompt ≤ 11,232 tokens | `prompt_size measure`, `cache_n + prompt_n` of the new A body | re-run window |
| AC-4 pinned | the FRE-1597 `fingerprint.json` pins, equal or noted | re-run window |

Items marked "re-run window" cannot be proved in this session. The build seat has no llama.cpp window.
