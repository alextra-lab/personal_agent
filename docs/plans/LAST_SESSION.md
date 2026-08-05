# Last session — 2026-08-05 (the day the owner drove, and master kept asserting the layer beneath)

## Doing / discussing

The identity thread closed end to end — the Susan-greeting bug is fixed, deployed and proven live on
its own phrasing. Around it the owner ran a day of model experimentation: the local 27B swapped in and
rolled back within the hour, then the OVH-hosted 27B added as a selectable cloud primary, which broke
twice before working. The session ended at a **deliberate clean break** the owner asked for so they
could pivot: nothing in progress, nothing awaiting deploy, both build seats idle. **Do not start
anything** — the owner names the next subject.

## What was decided and why

**Unsloth is the parameter authority for the local models, not the official Qwen card.** Owner's
ruling, and it is the right one: we run Unsloth's GGUF quant on llama.cpp and their guidance is written
for that path. It resolved a real incoherence — we were running temperature 0.6 (the cards' *coding*
preset) beside presence_penalty 1.5 (the *general* preset), a pairing no source recommends. Now exactly
one documented preset. The owner's server was separately at top_p 0.85 / top_k 10, which matches
neither source and is overridden by the catalog anyway; they know.

**Keeping the catalog KEY stable across model swaps was the decision that paid.** Master argued for
changing only the wire `id` and leaving `qwen3.6-35b-thinking` alone. That made both the swap and the
rollback one-line changes in each direction. The cost landed as predicted: the PWA renders the *key*,
so it read "35b" while the 27B answered. The durable fix — surface the id alongside the key in the
session-config endpoint — is deferred and belongs with Config Management.

**"Provider truth" does not transfer across providers.** The catalog convention of declaring real
provider ceilings (established for Anthropic/OpenAI) made every OVH call fail: OVH counts requested
*output* tokens against the context window, so `max_tokens: 262144` on a 262144 window leaves zero for
input. Master had recommended lowering it **on cost grounds** and framed a trade-off the owner could
decline — but it was not a trade-off, it was inoperable. The owner declined a recommendation whose real
justification was never given.

**Bounces are expensive — if master can fix a small thing, it should.** Owner's correction, mid-gate,
and it changed the outcome: master bounced PR #822 for an unproven AC, then merged it and took the
measurement itself. Carry this as a standing bias, not a one-off.

**The FRE-1053 census obligation was retired rather than chased.** A comment on that ticket from 07-31
already recorded that the claim the census existed to test was dead — the entity path was fixed by
other mechanisms — and recommended re-scoping. Nobody edited the body, so the obligation survived into
the build by inertia. Watch for that shape: a superseded requirement nobody deletes.

**Nothing in the recall scorer prefers episodes.** Verified in source against the owner's question: one
combine function, four weights, no branch on candidate kind. The roughly 2:1 admission is the sum of
three accidents — the empty-name topic hit (now fixed), a recency term at 0.20 that correlates with
kind by construction, and the 07-30 pair split that doubled one population against a fixed cap. That is
FRE-1158, filed and parked.

**Master's dominant failure, five occurrences, several caught by the owner.** Asserting a derived fact
without checking the layer beneath it. The 429 blamed on a busy endpoint when we were dispatching
*unauthenticated*; a claim that OVH's dedicated endpoint needed a client change when litellm reads
`OVHCLOUD_API_BASE` from env; a claim that the embedder's credential would flow to chat, read off the
config without checking the code path that consumes it; `preserve_thinking` floated as the
estimator-gap explanation when we never resend reasoning content at all; and a ranking probe that
scored on one subscore in isolation when it carries a tenth of the weight, producing a confident 76.7%
that was discarded rather than reported. Each time the instrument answered an adjacent question and
looked authoritative.

## Worktrees — anything special

- **build / build2** hold merged-but-undeleted branches, so `--delete-branch` fails locally every merge.
  Known, harmless.
- **build1 wedged twice today** at interactive prompts, hours apart, and invisible in dispatch state
  both times because a worktree-dirty refusal writes no record at all. Recorded on FRE-1077.
- **explore** stays pinned to the deployed SHA, not `origin/main`.

## Sequence position + drift

The whole day was owner-directed: the identity incident, then model work. **None of it touched the
console's standing four-item sequence** (telemetry residuals → Config Management → Linear async
feedback → Seshat Inference), which remains unstarted. That is drift only in the sense that live
incidents keep outranking it. Console untouched apart from one transcription; 39 of its 60 lines.

## Answers for the fresh start

- **Why is the `adr` stream wedged?** Deliberate. A stale dispatch-state entry names FRE-1127 as
  `launched` since 08-03 while that ticket sits in `Backlog`, so the daemon stalls every tick. Clearing
  it is one action and immediately dispatches FRE-1132 — a new subject, which the pause directive
  reserves for the owner.
- **Why is FRE-1122 `Approved` with no stream label?** Master moved it out of `Awaiting Deploy`: its
  fixture merged days ago and is live, so it never awaited a deploy — it awaits the baseline *run*,
  which needs owner authorization plus three recorded fixture defects fixed. The 9/20 partial is dead
  (produced under a different primary model) and must not be reused.
- **Is the OVH model safe as the daily driver?** No — as a fast option and for parallel evals, yes. It
  has **no prompt caching**, so re-transmission (~32K/call × ~25 calls) bills in full, roughly $0.40 a
  session, where the local model's cache absorbs it. Prompt-level defects are model-portable (FRE-1150
  reproduced on three model families), which is what makes it a good eval target.
- **Can it be compared to the local model in an experiment?** Not on parameters. The cloud path sends
  only `temperature` — no `top_p`, `top_k`, `min_p`, `presence_penalty`, and no `extra_body` at all.
