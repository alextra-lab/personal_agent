# Last session — 2026-07-27 (ADR-0125 accepted; the evidence contract shipped and immediately found a bug)

## READ THIS FIRST — the environment is UP, streams still OFF, gateway redeployed today

- `cloud-sim-seshat-gateway` rebuilt **12:00Z at `af29060d`**, health green on all five components.
  Owner authorised the rebuild explicitly (ask-first class).
- **`cloud-sim-embeddings` was re-stopped after the rebuild** (it revives every time — standing rule).
- **The six background LLM streams remain disabled in `.env` and must stay that way.** Nothing this
  session re-enabled anything. The summary sweep stays off; its two gates (FRE-994 bound — now done —
  and FRE-987 retry bound — still open) are not both met.
- The captures index template was registered **before** the rebuild, deliberately (see drift below).

## Doing / discussing (≤5 sentences)

The owner accepted **ADR-0125** and the evidence-contract chain went from proposal to shipped-and-verified
in one day: FRE-1000 (measurement gate) and FRE-1004 (the capture change) both closed Done, with FRE-1002
and FRE-1005 now building. The day's through-line was **instruments that lie** — a digest bound scored
against itself, two prompt hashes that cannot differ, producers inheriting the most expensive reasoning
config by omission, and a memory record that counted rather than named. FRE-1004's first live turn found a
real recall defect within five minutes, which is the best possible argument for the contract. Four new
tickets came out of verification rather than planning (FRE-1007, 1008, 1010, 1011). The session also
re-learned, twice, that a docs PR title carrying a ticket token corrupts the board.

## Commits — the story behind the last 10

- **PR #688 → FRE-994** — the compression curve. The headline is *the instrument is wrong, not the
  constant*: elasticity 0.16, so the prompt cannot deliver any bound tested, and at the deployed 250
  **47% of content-bearing digests are discarded**. Zero truncations in 100 calls, which **falsified
  FRE-993's founding premise**. $1.74 actual against $4.08 expected.
- **PR #689** — master's close-out. Its `--analyse` reproducibility check caught an error the build's own
  self-review had *introduced* while fixing a larger one: Amendment C said the uninstructed arm clears 90%
  at 450; it is 0.895 at 450 and 500, clearing only at 600. Precision, not significance — but the ADR
  adopts the number.
- **PR #691 → the owner accepted ADR-0125**, writing status `Approved`. That word is not in the project's
  vocabulary (83 Accepted / 0 Approved) and the index checker rejects it outright. **PR #692** normalised
  it to `Accepted` and applied the two on-acceptance consequences: index row, and ADR-0067
  reflection-surfacing → `Superseded by ADR-0125` (decision superseded, *code* not — that is FRE-1003).
- **PR #693 → FRE-1000** — the measurement gate. It **inverted the backing audit on both counts**: item 4
  (tool payloads) was flagged as the likely gap and is fine; item 6 (assembled context) was assumed fine
  and is the larger gap.
- **PR #694 → FRE-1004** — the capture change, +2680/-98. Verified live on the owner's turn at 12:04Z.

## Worktrees — anything special

- **build1** — `fre-1002-evidence-path-boundary-guard`, **freshly reset at 12:31Z** after a 3-hour stall
  (below). No context to preserve; it started clean.
- **build2** — stood down at 12:50Z. It was warm on FRE-1005 under `context:keep`, then found that
  ticket unbuildable (below) and stopped before writing code. Ticket parked; seat free to clear.
- **adrs** — idle on the merged `adr-0125-evidence-contract`; wants a fresh-start before its next ticket.
- **`master-914`** — still a stale worktree on the closed `fre-909-seat-rename`, the only reason that
  branch survives. Removal offered previously and not taken.

## Plan position + drift

MASTER_PLAN was rewritten this session and is current. ADR-0125 is now the live thread alongside the audit.

**Deliberate deviation, and it mattered.** FRE-1004's handoff runbook ordered *rebuild then register the
index template*. Master **reversed it** — today's captures index did not exist yet (404), so registering
first made the explicit mapping a guarantee rather than a race against the first write. Confirmed live
afterwards: `items` and `conversation_slice` are `nested`, which dynamic inference would have made
`object` — different query semantics, unfixable in place. Master also **declined to run
`setup-elasticsearch.sh`** (it applies 13 templates; only one was authorised) and PUT the single template
directly, which also avoids its back-attach step that the runbook forbids.

## Answers for the fresh start

- **Do NOT re-enable the summary sweep, and do NOT raise any budget cap.** Both standing. Amendment C's
  400 is *not* licence — FRE-987 is still open.
- **Two seats stalled today, silently, for hours.** build1 had **four** dispatch pokes sitting unsubmitted
  in its prompt buffer; build2 had one. Both needed a human to notice. This is **FRE-976's** subject, still
  parked, and it has now bitten ~6 times in two days. Master's own gap: it verified the *queue*
  (`--eligible` returned the ticket) and read that as healthy — **eligible ≠ started**. Check seat progress,
  not queue state.
- **The docs-PR token trap fired twice more** (FRE-994 knocked Done→Awaiting Deploy 2s after PR #689
  merged; FRE-993 dragged into Awaiting Deploy despite never being built). Both restored at this check.
  Now **11 occurrences / 7 sessions** — filed **FRE-1011** for the mechanical guard the memory has been
  asking for. Until it exists, the Awaiting-Deploy board check at reset is the only reliable catch.
- **FRE-1010 is the instrument's first find, and it is real.** On a melon-ice-cream turn, two ice-cream
  entities scoring 0.562/0.560 were recalled and dropped against an admitted 0.563. Mechanism confirmed:
  the task-assist render branch takes `ctx.memory_context[:3]` (score-blind positional cap) **and** reads
  `summary`/`user_message`, which entity payloads do not have — so a surviving entity renders as an **empty
  bullet**. The `"Ice cream"` entity's description literally answers the question asked.
- **A hole in ADR-0125's own contract, found by using it.** That empty-bullet entity was recorded
  `admitted: true` — correct by the ADR's definition (emitted, inlined, reached the wire) but the emitted
  content was empty. The contract cannot distinguish *emitted with content* from *emitted empty*. Master's
  view: `admitted` should require non-empty content; it is a small amendment, and it belongs to the next
  contract ticket, not folded into FRE-1010.
- **Reasoning config is inherited by omission.** Verified against litellm 1.89.2: with no effort hint
  litellm sends neither `thinking` nor `output_config`, so the provider default applies — on
  `claude-sonnet-5` that is adaptive thinking at **`high`**. `session_summary` and `insights` sit there by
  omission; only `captains_log` chose (`medium`). Owner direction: the parameter vocabulary is
  **provider-specific** and must be verified per provider through litellm, never copied from the Anthropic
  shape. **FRE-1007** owns the general rule.
- **FRE-999 is an umbrella sitting at `Needs Approval`** — by our own rules umbrellas belong in `Backlog`.
  Flagged to the owner, not yet moved.
- **Item 3 (reasoning trace) has no capture field at all** and is deliberately out of FRE-1004's scope. It
  needs a *feasibility* ticket first — on the bound Anthropic models the raw chain of thought is never
  returned, so "capture the reasoning trace" may mean capturing a summary and calling it evidence. Offered,
  not yet written.
- **THE BIGGEST FINDING OF THE DAY, and it arrived last. ADR-0098's Claim substrate is write-only.**
  build2 refused to build FRE-1005 because AC-4's fixture has no true-positive instance, and master
  verified all three legs: **91 Claims / 3 superseded**, **`HAS_FACT` is the only relationship type
  touching a Claim** (no edge to Entity or Turn), and **zero references to `Claim` in `request_gateway/`
  or `orchestrator/`**. Ninety-one claims written, three supersessions correctly adjudicated, **never once
  read into a turn** — ADR-0098's correctable-facts capability does not reach the model, because recall
  still runs entirely on the legacy Entity layer. Also proven absent: `corroboration_count` and
  `last_confirmed` have no populators anywhere. Owner decided to gate FRE-1005 behind claim-recallability
  and treat it as ADR work → **FRE-1012**. FRE-1005 parked (Approved, unlabelled); **FRE-1006 inherits the
  same premise** and must be decided with it.
- **The pattern to carry forward.** Four capabilities found in one day that exist, are correct, and never
  reach a consumer: structured output wired-and-unused (FRE-995), the prompt manifest built-and-discarded
  (FRE-1000), entities recalled-and-rendered-empty (FRE-1010), and the Claim substrate written-and-never-read
  (FRE-1012). That is ADR-0125's two-dimensions thesis validating itself four times over, and it suggests
  the standing question is not just *is it built* but *does anything consume it*.
- **A third stall variant, on top of the two above.** build1 ended its turn saying "I'll wait for the
  background codex review to complete" — the review had already finished in 32s, and nothing woke the seat.
  So the seat-stall family now has three shapes: unsubmitted prompt-buffer text, modal dialogs, and
  turn-ended-awaiting-a-completed-background-task. All three need a human to notice. All three are FRE-976.
