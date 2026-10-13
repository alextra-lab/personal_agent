# FRE-1603 — the routing and prompt-size re-run on Flash-Next (ADR-0156 A5)

**Study:** FRE-1603, 2026-10-13. **Seat:** build2. **Ticket ACs:** AC-2 (routing) and AC-3 (prompt size).
**Harness:** `scripts/eval/fre1597`, unchanged. **Plan:** `docs/superpowers/plans/2026-10-13-fre-1603-monitor-owner-catalog-lines.md`.
**Builds:** A is PR #1284 at `6fb9b5ac` (gateway `cb5a9175`). B is `b02503fc` with FRE-1488 (`c6447cc9`)
cherry-picked (gateway `94a517dd`).

This note records what was measured. It is a fresh measurement of one run. It does not prove a cause for any
single draw.

---

## Answer

| Ticket AC | Verdict | Measured |
|---|---|---|
| AC-2 — routing passes | **PASS** | All 7 skills meet their bound. 0 area confusions. 0 of 60 no-skill draws load a skill. One llama.cpp build |
| AC-3 — the prompt is smaller by at least 80% | **PASS** | A is 11,215 tokens against 11,232 (fixed bound) and 11,232 (same-window bound). The drop is 81.2% |
| AC-4 — pinned and reproducible | **PASS, with two noted differences** | Model, quant, server arguments, chat template and build equal FRE-1597. `return_progress` and the catalog differ, as noted |

## Method

Same as FRE-1597 (`docs/research/2026-10-13-fre-1597-routing-run.md`): the probe gateway with a recording stub
captures call 1 and call 2. The replay sends call 1 three times for each of the 49 items, then call 2 when call 1 is
exactly one `read_skill` of the expected skill. No tool runs. The loop is not re-run, because this ticket changes no
tool and no reveal.

| Part | Calls | Time (UTC) |
|---|---|---|
| Dry run, 2 items, 1 draw | 2 call 1, 2 call 2 | 15:38:06–15:39:21 |
| AC-3 measure | 3, cold (`cache_prompt: false`, `max_tokens: 1`) | 15:39:31–15:40:59 |
| Routing replay | 147 call 1, 79 call 2, 1 warm-up | 15:41:05–16:11:19 (30 min) |

**Retries.** No call was retried. The 147 rows and the warm-up hold no `extra.retried`, and no log mentions a
retry. The AC-3 report stores counts only, so its 3 calls have no retry field. No log line shows one.

## Pins (AC-4)

Equal to FRE-1597: model `unsloth/qwen3.8-flash-next`, quant `IQ4_XS - 4.25 bpw`, the server arguments (byte for
byte), the URL, the chat-template hash `12827f24…aadce`, the props file (sha256 `71d91c3e…`), build
`b11521-b42b7e6d3`, the draws (3), the tool definitions, and the hash of every scenario prompt (49 items).
`skills/*/evals.yaml` has no change against `origin/main`.

Two differences, both known before the run:

| Pin | Difference | Cause |
|---|---|---|
| Request sampling keys | One key added: `return_progress: true` | FRE-1488 (`c6447cc9`, PR #1280) reached main after FRE-1597 |
| System prompt hash | `8c15b750…` -> `5e748557…` | The catalog lines of this ticket |

**B deviates from the FRE-1597 B by the one request key.** B was captured again at `b02503fc` with `c6447cc9`
cherry-picked, so the B/A diff holds only T-C2 and this ticket's text (`unexpected: none`). The system messages and the
tools equal the FRE-1597 B. The non-system messages differ in the capture clock and in a random tool-call id, with
equal lengths, and the harness aligns A to B. B read 12,384 tokens, equal to the FRE-1597 B, so the flag does not
change the count.

## Findings

### F1 — AC-2 passes

| skill | draws | first | need | right | call 2 sent | FRE-1597 first |
|---|---|---|---|---|---|---|
| graph-inspection | 12 | 11 | 10 | 11 | 11 | 11 |
| interaction-outcomes | 9 | 8 | 8 | 8 | 8 | 8 |
| log-search | 15 | **13** | 13 | 13 | 13 | 9 |
| model-latency | 12 | 12 | 10 | 12 | 12 | 11 |
| service-health | 15 | 14 | 13 | 14 | 14 | 13 |
| spend-and-tokens | 12 | 11 | 10 | 11 | 11 | 12 |
| turn-explanation | 12 | 10 | 10 | 10 | 10 | 11 |

Area confusion: none. No-skill loads: 0 of 60.

`log-search` and `interaction-outcomes` pass at their bound with no slack. `turn-explanation` passes at its bound.
The sampling temperature is 1.0, and the run has 3 draws for each scenario. A repeat may differ by one draw.

**The monitor indices.** `log-search#2` (cache-erosion) is first on 3 of 3 draws, against 0 of 3 in FRE-1597.
`service-health#4` (the model-server monitor) is first on 2 of 3. Its miss called `search_memory`, not `log-search`.

**The remaining `log-search` miss.** `log-search#3` ("is the joinability index family daily or monthly?") is first on
1 of 3. Two draws went straight to `bash` with no skill, as in FRE-1597 (2 of 3). `log-search#4` (ES|QL) is now 3 of 3,
against 2 of 3. The run does not show why `#3` persists.

**The other misses (5 draws):** `graph-inspection#1` called `search_memory`. `turn-explanation#1` called
`search_memory` then `recall_personal_history`. `turn-explanation#3` called `recall_personal_history`.
`interaction-outcomes#0` called `recall_personal_history`. `spend-and-tokens#3` made no tool call.

**Shortened lines.** `spend-and-tokens` and `turn-explanation` had phrases removed from their descriptions, and each
lost one draw against FRE-1597 (12 -> 11, 11 -> 10). `model-latency` also had a phrase removed and gained one
(11 -> 12). One run cannot tell an effect from noise.

### F2 — AC-3 passes

| Body | Tokens (`cache_n + prompt_n`, `cache_n` 0) |
|---|---|
| B (b02503fc + FRE-1488) | 12,384 |
| B-minus (B without `query_telemetry`) | 10,944 |
| A (PR #1284) | 11,215 |

- The definition is 1,440 tokens. The drop is 1,169 tokens: 81.2%. The bound is 80%.
- Fixed bound: A at most 11,232. A is 17 below it.
- Same-window bound: B − 0.8 × (B − B-minus) = 12,384 − 1,152 = 11,232. B read the same as FRE-1597, so the two bounds are
  equal. A is 17 below it.
- The first build of this PR measured 11 estimated tokens against a need of 10. The preflight showed it, and the
  `log-search` line was cut again before the run. Estimated catalog change against `a02c4aeb`: minus 26 tokens. Real
  change against the FRE-1597 A (11,242): minus 27 tokens.

## Threats to validity

- **One run, 3 draws for each scenario, temperature 1.0.** Three skills pass at their bound.
- **B differs from the FRE-1597 B by one request key.** It does not change the count, which the run shows (12,384
  in both).
- **Call 2 after another first reply is not measured.** As in FRE-1597.
- **One no-skill prompt for AC-3.** As in FRE-1597.
- **The loop is not re-run.** AC-2 and AC-4 of FRE-1597 hold for the tools, which this ticket does not change.

## Data

Run directory (git-ignored locally, holds full prompts, never to be committed):
`telemetry/evaluation/fre1603-routing-run/20261013-1519/`: `routing/rows.jsonl`, `routing/report.json`,
`routing/fingerprint.json`, `routing/warmup.jsonl`, `prompt_size/report.json`, `prompt_size/B.json`,
`prompt_size/B.fre1597.json`, `prompt_size/A.json`, `pins/`, `preflight.json`.

```bash
uv run python -m scripts.eval.fre1597.routing score --run-dir telemetry/evaluation/fre1603-routing-run/20261013-1519
```

**References**
- FRE-1597 note: `docs/research/2026-10-13-fre-1597-routing-run.md`
- ADR-0156 A4, A5, AC-F1, AC-F4: `docs/architecture_decisions/ADR-0156-skills-and-tools-follow-the-industry-standard.md`
- FRE-1488, `return_progress` on the local call: commit `c6447cc9`
