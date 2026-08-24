# Last session — 2026-08-24

## Doing / discussing  (≤5 sentences)

The owner reported severe hallucination on a cooking question and the whole session went to chasing
it, ending somewhere neither of us expected: the injected prompt surface is tilted toward recall,
and a gitignored `.env` override had been running the skill router in a mode ADR-0066 explicitly
parked. Both are fixed and the failure no longer reproduces. Two decisions are still owed by the
owner — FRE-1292 (an alarm for a producer that stops entirely) and FRE-1294 (the Anthropic spend
limit, which resets 2026-09-01 and will otherwise recur). The ADR-0138 implementation chain
(FRE-1280..1287) remains Needs Approval and untouched, deliberately waiting on this result.

## What was decided and why

**The first diagnosis was wrong, and the correction is the session's main product.** FRE-1278 said
the search trigger was keyed to recency and therefore never fired on questions the model simply
does not know. True but secondary. The larger cause is that *every* injected surface points inward:
22 skills, six teaching recall, **zero** teaching web search; the tool-awareness renderer spelling
out `memory` in full while collapsing the 8-tool `network` category behind an ellipsis; a
`web_search` description reading as a utility and closing by redirecting to `perplexity_query`; and
the Known-Entities block labelling its contents "entities drawn from earlier conversations" while
forbidding the model to say it has no memory. FRE-1290 fixed the first three; the fourth is
ADR-0138 D6 and deliberately untouched, which is what made FRE-1290 a clean test.

**`model_decided` was never an improvement, and running it was config drift.** ADR-0066 D1 decided
`hybrid`, on measured evidence: same correctness, no extra LLM call, self-describing skill
authoring. D2 gates `model_decided` on injection exceeding 6,000 tokens. The index measures **982**
— 16% of the bar. The code default was still `hybrid`; `.env:621` overrode it, with no commit,
author or ticket. Owner reverted it. The cost was concrete: ~750 ms per turn for nothing, a router
right 78–83% of the time replacing keyword matching that was "always sufficient", and — decisively
— it removed the `read_skill` recovery path. ADR-0066 credits `model_decided` with eliminating 40–50%
`read_skill` fallback as a *win*; that fallback **was** the primary model noticing the router had
died and compensating. Optimising it away removed the system's own error signal.

**Master's own measurement was invalid three ways, and the revert it produced was unjustified.**
Probes wrote 170 group-visible entities that then suppressed `web_search` on later probes — the
measurement poisoned what it measured. The pre/post arms differed in *channel* as well as code. And
both ran inside a two-day Anthropic quota outage. On that evidence master reverted PR #942 and told
the owner it regressed. It did not; the number measured contamination. FRE-1278 is Canceled with
this recorded, and the rules stay recoverable from #942 should a clean measurement ever want them.

**`skills_loaded` was never measuring what three separate analyses thought.** It reads only
`ctx.loaded_skills`, which `hybrid` and `keyword` never write. So it was empty under the *default*
mode while skills were being injected — read once as "no skill matched", once as "the router is
dead", once as "hybrid isn't injecting". All three were wrong. FRE-1291 fixed it via FRE-1004's
existing evidence record (read-side only, dedup key untouched by construction). **Any
`skills_loaded` value before `55ee7d2c` under hybrid/keyword is under-reported.**

**The $100 Anthropic bill was not Seshat and never was.** Two keys: `claude-cli` (Opus 4.7, ~$95)
and `AGENT_ANTHROPIC_API_KEY` (Sonnet-5 + Haiku-4.5, $6.07/285 calls). Seshat consumed 6% of the
spend and absorbed 100% of the outage when the shared limit exhausted. Proven, not inferred:
disabling one key left the other returning HTTP 200.

## Worktrees — anything special

Two pre-existing stash entries in `.claude/worktrees/build` belong to neither master nor the
current build seat (one is `fre-916-adr-0121-t1-catalog-providers`). The seat hit them with a bare
`git stash pop`, pulled a stranger's WIP into its tree as `UU` conflicts, recovered cleanly, and
wrote itself the rule. **Do not bare-stash in that worktree.**

## Sequence position + drift

Full session off the console's standing sequence (telemetry residuals → Configuration Management →
Linear async feedback → Seshat Inference), and correctly so — the owner reported a live quality
failure in the thing the project exists to be. FRE-1007 (Configuration Management) did ship. The
ADR-0138 chain is the sequence's next real move and awaits owner approval.

## Answers for the fresh start

**Is the hallucination fixed?** The reported failure no longer reproduces — sardines-in-Portugal now
returns real brands, real retailers, DECO Proteste and a cited NYT ranking. Improvement was *not*
demonstrated as a rate: the pre-change baseline was already 5/5 (ceiling effect) and both arms ran
during the quota outage. Verified working; not statistically proven better.

**Why is FRE-1278 Canceled rather than Done?** Nothing it shipped is in `main`. Master reverted it on
a bad measurement, and the underlying problem was fixed by FRE-1290 instead.

**Can I trust `skills_loaded` in historical data?** Not before `55ee7d2c`, and only for
`model_decided` / explicit `read_skill` even then.

**Why is the ADR-0138 chain still Needs Approval?** Deliberate. FRE-1290 was the cheap test of its
thesis, and it's architecture — the owner's approval, not master's Tier-3 grant.

**Anything time-boxed?** FRE-1294. The Anthropic limit resets 2026-09-01; without a decision the
same silent two-day outage recurs.
