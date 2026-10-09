# FRE-1517 stage 2c — FRE-1561 A/B, pre-registered before the stage-2c session

Owner, 2026-10-09: file FRE-1561 and prioritize it. Master, the same day: the build seat delivers the
setting (PR #1232), and explore runs the A/B under FRE-1517 as stage 2c, with
`stage2_gemma_planner_off` (session `67bcc570`) as the control. Everything not named here follows
`blind_scoring_stage2.md`.

## The arms

| Arm | Session | Gateway setting |
|---|---|---|
| Control: `stage2_gemma_planner_off` | `67bcc570` (stage 2b) | `AGENT_SUB_AGENT_RESEARCHER_MIN_SEARCH_ROUNDS` 0 |
| Variant: `stage2c_gemma_min5` | new | `AGENT_SUB_AGENT_RESEARCHER_MIN_SEARCH_ROUNDS=5` |

The two gateway builds must differ only by PR #1232 and the setting. The stage-2c report names the
`src/` diff between the two HEADs.

## What decides the hypothesis — stated before the run

The hypothesis: the variant makes Gemma's `standard` researcher workers search more, and that makes
its research turns (2, 5 and 8) better.

1. **Rounds (FRE-1561 AC-3).** Per worker, `tool_iterations` from the sub-agent captures. Per fan-out
   turn, the `web_search` count. The control's values: workers 3, 1 · 1, 3 · 1, 4, 1. Web searches
   3 · 6 · 6.
2. **The variant reaches the worker (AC-2, live).** Each `standard` researcher capture of the variant
   arm shows a `system_prompt_chars` larger than the control's by the variant's length (220 chars at
   n=5). Read from `sub_agent_captures.system_prompt_chars`.
3. **Delivery and time (AC-4).** Part 1 delivered, and wall time, per turn.
4. **Quality (AC-5).** One blind pool of five sheets, new codes, two new scorers: the variant, the
   control (`67bcc570`), the Flash-Next control (`ad0d090b`) and the two 2026-09-15 Sonnet anchors.
   The decisive figure is Q on turns 2, 5 and 8, variant against control, per scorer and as a mean.

**Reading rule, fixed now.** The hypothesis is supported only if both scorers give the variant a
higher Q sum on turns 2, 5 and 8 than the control. One scorer higher and one equal or lower is
"no difference measured". One replicate cannot show more than a direction.
