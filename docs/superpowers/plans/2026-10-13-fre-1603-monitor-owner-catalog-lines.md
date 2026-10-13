# FRE-1603 — one owner for the monitor indices, trigger words first, shorter catalog lines

**Ticket:** FRE-1603 (ADR-0156 A5). **Source:** FRE-1597 note `docs/research/2026-10-13-fre-1597-routing-run.md`,
F1, F3 and Proposals 1–3. **Owner decision:** 2026-10-13, "Yes" on the three proposals, 80% target kept.
**Class:** Standard (skill text ships in the gateway image and drives routing). **Deploy:** `seshat-gateway` rebuild.

## Scope

| # | Change | Files |
|---|--------|-------|
| 1 | `log-search` owns the monitor indices. `service-health` loses them from its description and body, and its "Not for…" line names `log-search` | `skills/service-health/SKILL.md`, `skills/log-search/SKILL.md` |
| 2 | The `log-search` description puts its trigger words first: log events, counts, an index listing, ES\|QL | `skills/log-search/SKILL.md` |
| 3 | The A4 descriptions are at least 10 tokens shorter in total. Each keeps its objective and its "Not for…" clause | `skills/{log-search,service-health,model-latency,spend-and-tokens,turn-explanation}/SKILL.md` |
| 4 | Re-run AC-1 routing and AC-3 prompt size with the FRE-1597 harness and pins, in a window from master, after the owner OKs the preflight | `scripts/eval/fre1597/*` (no change), run directory |

No tool, no tool definition and no `provides-tools` line changes. `interaction-outcomes` and `graph-inspection`
descriptions are not touched: `interaction-outcomes` passed with no slack (8 of 9).

## Assumptions and risks (stated before coding)

1. **`service-health#4` has no slack.** "Is the local model server healthy according to its monitor?" expects
   `service-health`. `service-health` passed 13 of 15 with a bound of 13. After item 1 its body no longer reads
   `agent-monitors-slm-health`, so the 3 draws of `#4` may go to `log-search`. The ticket forbids changing a scenario to
   fit the result, so the re-run decides. The preflight note tells the owner this before the window opens.
2. **`service-health` keeps `provides-tools: search_logs`.** The ticket says no reveal changes, ADR-0156 A4 lists the
   reveal, and `test_telemetry_tools_visibility.py::test_the_providers_are_the_skills_that_the_adr_names` pins it.
   After item 1 the body no longer uses the tool, so the reveal is dead weight. The handoff proposes removing it with
   an ADR-0156 amendment. This PR does not.
3. **Sampling noise.** Temperature is 1.0 and three skills sit at the bound (13 of 15, 13 of 15, 8 of 9). A miss on
   the re-run may come from noise. The report states the draws, not only pass or fail.
4. **Characters stand in for tokens until the re-run.** FRE-1597 measured 1,340 characters = 298 tokens, 4.5 characters
   per token. The bound is 10 tokens, about 45 characters. The build saves 142 characters, about 32 tokens.
5. **YAML.** A description is an unquoted scalar, so it cannot hold `: `. The first draft of the `log-search` line did,
   and 22 validator cases failed. The line uses " - " as the other lines do.

## Steps

Every step is one commit unless it says otherwise. Run all commands from this worktree.

### 1. Failing tests first

Create `tests/docs/test_monitor_index_owner.py` (17 cases):

- `service-health` description: the part before "Not for" holds neither "monitor" nor "pipeline". (AC-1)
- `service-health` "Not for…" line holds "monitor" and `` `log-search` ``. (AC-1)
- `service-health` body holds no `agent-monitors-`, no `search_logs(` call and no heading with "monitor". (AC-1)
- `log-search` keeps "monitor indices" in its claim and `agent-monitors-slm-health` in its body. (guard)
- `log-search` body holds the stale-document rule that `service-health` gave up. (fold-in)
- `log-search` first sentence holds "log events", "counts", "indices" and "ES|QL", one case each. (item 2)
- The seven A4 descriptions total at most `3738 - 120` characters. 3,738 is the FRE-1597 build. 120 characters is
  about 27 tokens at 4.5 characters per token, against the 10 tokens that AC-3 needs. (item 3)
- Each A4 description keeps a "Not for…" clause that names a skill in backticks. (guard)

Run: `uv run pytest tests/docs/test_monitor_index_owner.py -q`. Result before the edits, observed: 6 failed, 11 passed.
The 11 that pass are guards: they hold today and must hold after. Only the 6 prove the change.

### 2. Edit the skills

- `skills/service-health/SKILL.md`:
  - description: remove the two monitor clauses and "or a pipeline". Add `log-search` to the "Not for…" line for the
    monitor indices.
  - body: delete the Rules bullet on monitor indices, "Step 3 — The monitor indices", the Success bullet on monitor
    answers, and the Recovery row "A monitor query returns no document". Renumber nothing else.
  - add one line to the Rules: a monitor index or a pipeline question goes to the `log-search` skill, loaded with
    `read_skill`.
- `skills/log-search/SKILL.md`:
  - description: rewrite with the trigger words first (see the test).
  - body: add the two rules that moved from `service-health`. A monitor index records what a monitor saw at its last
    run, and a newest document older than the monitor's interval is reported as stale. Put them in Rules and Success.
- `skills/{model-latency,spend-and-tokens,turn-explanation}/SKILL.md`: drop redundant phrases in the description
  only. No trigger word and no "Not for…" clause is removed.

Run: `uv run pytest tests/docs/test_monitor_index_owner.py -q`. Expected: 5 passed.

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
