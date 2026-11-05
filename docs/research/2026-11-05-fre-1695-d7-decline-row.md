# FRE-1695 — The D7 decline row on b12138, and a row that separates a regression from chance

**Date:** 2026-11-05 · **Seat:** cc-2build · **Ticket:** FRE-1695 · **Backing ADR:** ADR-0154 D7 and Amendment 3 ·
**Earlier study:** FRE-1694 (six prompt arms, 5 trials per fixture, b11751)

The owner approved a measurement on the current build, with a noise arm. A later Laya test uses these draws as its
baseline. The run used the decision-only replay (`scripts/eval/fre1537 replay --arms decide`) on the owner's Mac.
It made no paid call and it built no new harness.

## Summary

| Question | Answer |
|---|---|
| Did the decline rate drop on b12138? | No. The shipped prompt declined correctly on 186 of 200 draws (93%, interval 89–96%). On b11751 the same two shipped prompts scored 88% (82–92%). |
| What did change on b12138? | Expand-correct fell to 223 of 320 draws (70%, interval 64–74%). On b11751 it was 89% (85–92%). The D7 floor is 43 of 48 (90%). |
| How large is the spread between two runs of one prompt? | At 10 trials per fixture, 2.0 points on decline-correct and 3.1 points on expand-correct. Both sit inside sampling error. |
| Does V2 beat the shipped prompt? | No. V2 differs by +1.0 point on decline-correct and +1.6 points on expand-correct. Both are inside the gap. |
| Does a digest damage the decision? | No digest lowered decline-correct. A relevant digest raised expand-correct on four fixtures by 26 points. |
| What D7 decline row is proposed? | At least 88 of 100 decline draws (10 trials on each of the 10 fixtures). False-fail rate 1.4% at the measured 93%. |
| Fixture and message? | Change the `tool_logs` fixture. Leave the "Strategy: HYBRID" line. |

The expand result is the finding that needs an owner decision. It is outside the four questions of the ticket.
The planner now answers `SINGLE` on 97 of 320 expand-fixture draws (30%). On Oct 25 it did so on 8 of 80 draws (10%).
The planner input is byte-identical to the Oct 25 input. Only the engine and its model file changed.

## Method as run

- **Build (AC-1).** Master granted the window. The build string read `b12138-f7b41cca4` from the `system_fingerprint`
  field of the replies, and the fingerprint file records it. A proxy in front of llama.cpp returns 403 for `/props`,
  so the probe did not read that route. Master accepted the deviation. `slm_server` read `/props` at the start of the
  hold and reported `b12138-f7b41cca4`. Its FRE-1738 capture shows the same string in `system_fingerprint`.
- **Inputs.** Capture and render ran on `origin/main` at `abb9589e`, which includes FRE-1740. The rendered prompt hash is
  `d13f0dca…` (the shipped C1 prompt, ADR-0154 Amendment 3). The 27 fixtures and their user messages equal those of the
  FRE-1694 study byte for byte. The planner mode, the sampling and the quant (`UD-IQ4_XS`) equal those of the FRE-1694
  C1 run. The fingerprints differ only in engine build and git head.
- **Runs.** Each run used `--mode planner --arms decide --trials 10`. That gives 100 decline draws and 160 expand draws
  per run, plus follow-up fixtures inside those counts. `c5_noverb` is not scored. The order was C1 run 1, C1 run 2
  (the noise arm), V2, the unrelated digest, then the relevant digests. Each full run took 19 to 25 minutes.
- **V2.** V2 is the FRE-1694 V2 system text (`41d2c9ea…`), copied from the FRE-1694 run directory into a copy of the new
  `prompts.json`. Its list of the primary's 22 tools equals the 22 tools of the captured primary request, so it needed
  no rebuild from the registry.
- **Counting.** A decline is `SINGLE` with no task. An expansion is `HYBRID` or `DECOMPOSE` with at least one task.
  Anything else is invalid and counts as incorrect (ADR-0154 D2). Intervals are 95% Wilson intervals. A difference
  between two rates uses the Newcombe interval.
- **Result.** All 12 replays ended with exit 0 and no error row. Reasoning characters were 0 on every call. The
  scorer `score.py` marks any run with other than 30 decline draws as FAIL by design. So these numbers come from the raw
  rows. They use the scorer's own `classify`, `wilson` and `rate_text`.

## AC-2 — two runs of the shipped prompt

| Run | Decline-correct | Expand-correct | Invalid plans | Declined call p50 |
|---|---|---|---|---|
| C1 run 1 | 92/100 = 92% (85–96%) | 109/160 = 68% (61–75%) | 1 | 0.78 s, 13 tokens |
| C1 run 2, the noise arm | 94/100 = 94% (88–97%) | 114/160 = 71% (64–78%) | 1 | 0.74 s, 13 tokens |
| Both runs | 186/200 = 93% (89–96%) | 223/320 = 70% (64–74%) | 2 | — |

The gap between the runs, run 1 minus run 2:

- Decline-correct: −2.0 points (−9.7 to +5.6). Sampling alone gives a standard deviation of 3.6 points. The observed z is −0.55.
- Expand-correct: −3.1 points (−13.1 to +6.9). Sampling alone gives 5.1 points. The observed z is −0.61.

The two runs agree within sampling error. One pair of runs cannot show a drift between runs that is smaller than this. Five
fixtures differ by three or more draws of 10 between the two runs. All five are expand fixtures. The correct draws of
run 1 and run 2 were: `c1_tool_imperative` (5 and 8), `c3_paragraph` (2 and 6), `c4_deliverable` (4 and 0),
`gpsr_tuna` (6 and 3) and `regulatory` (6 and 9).
Every decline fixture except `tool_logs` and the three `susan` fixtures declined 10 of 10 in both runs.

The decline misses of the two runs total 14. `tool_logs` holds 6, `susan_2_able_to_run` 3, `susan_3_thought_i_asked` 3 and
`susan_4_build_and_run` 2.

## AC-3 — V2 against the gap

| Run | Decline-correct | Expand-correct | Invalid plans | Declined call p50 |
|---|---|---|---|---|
| V2 | 94/100 = 94% (88–97%) | 114/160 = 71% (64–78%) | 2 | 0.84 s, 19 tokens |

V2 against the pooled C1 runs: decline-correct +1.0 point (−6.1 to +6.5) and expand-correct +1.6 points (−7.3 to +9.9).
V2 against each C1 run stays inside the same limits. Every difference is smaller than the interval of the gap. The data
shows no gain from V2. The shipped prompt was not changed. The owner picks any variant.

`tool_logs` declined 9 of 10 draws under V2 and 14 of 20 under C1. That difference has an interval wide enough to
include zero. FRE-1694 saw the same direction (5 of 5 against 3 of 5).

## AC-4 — did the decline rate drop?

The rows below come from every local decision run on disk, grouped by engine build. The b11689 run of 2026-10-20 has no
rows on disk, so it is not in the table.

| Build | Runs | Decline-correct | Expand-correct |
|---|---|---|---|
| b11098 and b11521 (Oct 3 to Oct 12) | 8 | 216/221 = 98% (95–99%) | 340/360 = 94% (92–96%) |
| b11751 (Oct 25), all prompts | 9 | 349/390 = 89% (86–92%) | 543/624 = 87% (84–89%) |
| b11751, the two shipped prompts only | 5 | 167/190 = 88% (82–92%) | 270/304 = 89% (85–92%) |
| b12138 (Nov 5) | 3 | 280/300 = 93% (90–96%) | 337/480 = 70% (66–74%) |

- b11751 against the earlier builds: decline-correct −8.3 points (−11.9 to −4.3). The data supports a drop of the decline
  rate on b11751. The ticket suspected this.
- b12138 against b11751: decline-correct +3.8 points (−0.5 to +8.0), or +5.4 points (+0.3 to +11.3) against the two
  shipped prompts. The data does not support a further drop on b12138. The rate sits between the two earlier groups.
- b12138 against b11751: expand-correct −16.8 points (−21.7 to −12.0). This is about five times the gap of AC-2.

**The limit.** The builds were not measured side by side. Each group holds several different prompts. The earlier runs
used 3 or 5 trials per fixture, and these runs used 10. The production move of 2026-11-05 may also have changed the model
file. The fingerprint cannot show this, because the model path reads `unknown`. So the data cannot separate the engine
from the model file. Runs of one prompt differ by more than their intervals show, so the earlier groups carry more
spread than the table shows.

## AC-5 — a D7 decline row that separates a regression from chance

**Proposal.** The decline row reads: at least **88 of 100** decline draws, from 10 trials on each of the 10 decline
fixtures. This replaces "at least 28 of 30". A threshold change needs an owner-approved amendment of ADR-0154. This
document proposes it and applies nothing.

The false-fail rate and the catch rate come from the exact distribution of the number of correct draws. The distribution
uses the measured decline rate of each fixture, pooled over the two C1 runs. The mean is 93.0%.

| Row | False-fail at the measured 93% | False-fail at a true 90% | Catches a 5-point drop | Catches a 10-point drop |
|---|---|---|---|---|
| Today: 28 of 30 | 35% | — | — | — |
| 3 trials, 30 draws, at least 26 | 4.0% | 16.4% | 28% | 60% |
| **10 trials, 100 draws, at least 88** | **1.4%** | 18.6% | 42% | 90% |
| 10 trials, 100 draws, at least 87 | 0.5% | 11.1% | — | — |
| 20 trials, 200 draws, at least 176 | 0.2% | 13.2% | 45% | 97% |

- Today's row fails a configuration that declines correctly 93% of the time in about one run of three. That is the coin
  toss that the ticket describes.
- The proposed row passes a 93% configuration in 98.6% of runs. If the rate of every fixture drops by 10 points (mean 83%),
  the row fails 90% of runs.
- The proposed row cannot separate a 5-point drop from chance. No row at this cost can. A 5-point drop is caught in 42% of runs.
- The row is sensitive to the measured rate. A true rate of 90% raises the false-fail rate to 18.6%.
- The calculation treats draws as independent given the fixture. The noise arm agrees with this (z of −0.55), but one
  pair of runs cannot rule out a drift between runs.
- The cost is 10 trials on each fixture. A full run of 270 draws took 19 to 25 minutes in this study.

If the owner drops `tool_logs` (AC-6), the decline set holds 9 fixtures with a measured mean of 95.6%. The equal row is at
least **82 of 90** (false-fail 1.3%, catches a 10-point drop in 92% of runs).

### Other D7 rows that this data shows

These rows are outside AC-5. They are listed so that the owner sees them before any amendment.

- **Expand-correct, at least 43 of 48.** The shipped prompt on b12138 scores 70% and fails by a wide margin. The same
  weakness of the row applies: 48 draws are too few. A row of equal design needs its own proposal.
- **Plans that fail to parse or validate, 0.** Three of four runs hold 1 or 2 invalid plans. All four plans in these runs
  are `HYBRID` with no task, on a decline fixture (`susan_2`, `susan_3` and `susan_4`). The invalid rate is 4 of 400
  decline draws (1%) over the two C1 runs, V2 and the unrelated-digest run. The unrelated-digest run and the eight
  relevant-digest runs hold none. In production an invalid plan returns the turn to the tool
  loop, the same effect as a decline. The row of 0 will fail a qualified configuration by chance.

## AC-6 — the `tool_logs` fixture and "Strategy: HYBRID"

**Recommendation: change the fixture, and leave the message.**

- **The fixture premise does not hold.** The ticket says the primary can reach `search_logs`. The captured primary request
  holds 22 tools, and `search_logs` is not one of them. Only a worker holds it. The planner does see `search_logs` in its
  worker tool list. So the label "decline" for a log check rests on a premise that the capture contradicts. The primary
  holds `bash`, `read` and `run_python`, which can reach files, but it has no log search tool.
- **The fixture is the weakest decline fixture.** `tool_logs` declined 14 of 20 draws. It holds 6 of the 14 decline
  misses.
- **The best action.** Remove `tool_logs` from the scored decline set, or relabel it as "either answer is correct". The
  change alters the D7 denominators, so it belongs in the same ADR-0154 amendment as AC-5.
- **The message.** The line "Strategy: HYBRID" appears on every planner user message. It echoes into the output in 4 of 400
  decline draws (1%), as `HYBRID` with no task. No arm measured the effect of removing the line. A change to the
  message is a routing change under D7 and needs a new qualification on a gate that is already noisy. The measured cost
  of the line is 1% of decline draws that end as a tool-loop return, which is the same effect as a decline. Leave it.
  An arm that removes the line costs one 25-minute run with a copy of `prompts.json`, if the owner wants the data.

## AC-6b — the two digest arms (ADR-0154 D7 and AC-8)

FRE-1740 changes the planner input on turns with a digest, and it does not change the prompt hash. The digests are
synthetic. The production builder `_build_planner_memory_digest` made them. No description says "the user", because the
production line fit cuts such a description to an empty text.

| Arm | Fixtures and draws | Decline-correct | Expand-correct | Declined call p50 |
|---|---|---|---|---|
| Unrelated digest, 6 lines on pastimes | all 27, 10 trials | 93/100 = 93% (86–97%) | 121/160 = 76% (68–82%) | 1.44 s, 22 tokens |
| Pooled C1, no digest | all 27, 10 trials | 186/200 = 93% (89–96%) | 223/320 = 70% (64–74%) | 0.76 s, 13 tokens |
| Difference | — | +0.0 points (−7.3 to +5.7) | +5.9 points (−2.7 to +13.9) | +0.7 s |

| Arm | Fixtures and draws | With digest | Without digest, C1 runs 1 and 2 | Difference |
|---|---|---|---|---|
| Relevant digest, 2 lines each | 4 decline fixtures, 40 draws | 40/40 = 100% (91–100%) | 80/80 = 100% (95–100%) | +0.0 points (−8.8 to +4.6) |
| Relevant digest, 2 lines each | 4 expand fixtures, 40 draws | 33/40 = 82% (68–91%) | 45/80 = 56% (45–67%) | +26.2 points (+8.5 to +40.2) |

- The relevant digests were for `heatpump`, `gpsr_tuna`, `c4_deliverable`, `mallorca` (expand) and `susan_1_run_tool`,
  `greeting`, `boiler_decline`, `tariffs_decline` (decline). Each bears on the question and does not change the right answer.
- Neither digest arm lowered decline-correct. Both stay inside the gap of AC-2.
- The unrelated digest moved expand-correct by +5.9 points. That is larger than the gap of 3.1 points, and its interval
  includes zero.
- The relevant digest moved expand-correct on four fixtures by +26 points. This is far outside the gap. The digest
  helps in this sample because the planner under-expands on b12138. With 4 fixtures and one digest wording each, the
  size of the effect is not firm.
- AC-8 part 3 of the ADR fails a run that changes the decision on more than 1 of 24 draws (8 fixtures, 3 trials). At 10
  trials that count does not apply. The change here moves toward the correct label. The owner reads this against the
  intent of AC-8, which is "does not damage the decision".
- The unrelated digest adds 0.7 s to the median declined call (1.44 s against 0.76 s). That stays under the D7 limit of 2 s.
  Declined completion tokens rise to 22, under the limit of 40.

## AC-7 — where the draws are

The directories are git-ignored, because they hold full prompts. No fixture text is in this document beyond the labels
that the repo already holds. The draws are in the run directory on the VPS:

- `telemetry/evaluation/fre1537-planner-probe/20261105-fre1695/rows/c1-r1/decide.jsonl` (C1 run 1)
- `telemetry/evaluation/fre1537-planner-probe/20261105-fre1695/rows/c1-r2/decide.jsonl` (C1 run 2, the noise arm)
- `telemetry/evaluation/fre1537-planner-probe/20261105-fre1695-v2/rows/v2/decide.jsonl` (V2)
- `telemetry/evaluation/fre1537-planner-probe/20261105-fre1695/rows/c1-digest-unrelated/decide.jsonl`
- `telemetry/evaluation/fre1537-planner-probe/20261105-fre1695/rows/c1-digest-rel-<label>/decide.jsonl` (8 files)
- `telemetry/evaluation/fre1537-planner-probe/20261105-fre1695/rows/preflight/decide.jsonl` (2 draws, not part of any figure)
- Each `rows/<tag>/` directory also holds `fingerprint.json`. The digests are in `20261105-fre1695/digests/`.
- `telemetry/evaluation/fre1537-planner-probe/20261105-fre1695/analysis/` holds the analysis scripts, the window script and
  their output (`analysis1.txt`, `analysis2.txt`, `analysis3.txt`). They are scratch files, not harness code.

The captured primary requests and the rendered prompts are in `20261105-fre1695/captured/` and `prompts.json`.

## Deviations and limits

- `/props` was not readable. The build came from `system_fingerprint`. Master accepted this.
- The first render attempt exited 137 (killed). One retry with no change passed. The cause is not proven. The gateway
  container has a 768 MiB limit, and it used 436 MiB before render. The tracked code is unchanged.
- The model file may have moved together with the engine on 2026-11-05. The data cannot say which one moved the expand rate.
- One pair of runs is a thin estimate of the spread between runs. It agrees with sampling error and nothing more.
- The decision-only replay does not run the paired or long-history arms. It does not run a full turn.
- The earlier-build comparison is not side by side. See AC-4.
- Temperature is 1.0, so every draw is a sample.

## Decisions for the owner

1. The expand drop. The shipped prompt on b12138 expands 70% of expand-fixture draws, against 89% on b11751. The cause is
   the engine or the model file. The D7 expand row fails by a wide margin.
2. The AC-5 row, and whether to apply the same design to the expand row and the zero-invalid row. Each needs an ADR-0154
   amendment.
3. The `tool_logs` fixture (AC-6).
4. The digest wording in AC-8 part 3, because the relevant digest moves the decision toward the correct label.
