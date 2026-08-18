# Last session — 2026-08-17

## Doing / discussing  (≤5 sentences)

Gated the tail of the FRE-1269 PWA arc — rounds 9 through 12 — and closed it Done on the owner's
words, "The results are acceptable." Nothing is in flight: no open PRs, no unconsumed triggers,
build1 idle, adr's head is the newly-activated FRE-1087 seam. Four tickets were filed on the way
out and all sit unlabelled, so none of them is dispatched. The next session starts clean and should
read the console's sequencing directive rather than continuing the PWA thread.

## What was decided and why

**I escalated a keyboard regression that does not exist, four times, and the owner corrected me.**
Build1 reported the owner had hit a keyboard-pan bug and measured `scrollY: 376` with the shell
height unchanged. I filed it Urgent (FRE-1272), raised it in four consecutive briefings, and offered
a production revert. The owner: "There is no keyboard regression." Two errors, both mine. The
owner's *actual* words were "the input is too tall by default" — a composer-height complaint, which
rounds 11 and 12 fixed. Build1 layered a keyboard-pan theory on top and I acted on the layer without
reading back to the source. Second, a page scrolling when the keyboard opens is ordinary iOS
behaviour; I treated a number that was anomalous against my model as a defect. **A seat's paraphrase
of the owner is not the owner.** FRE-1272 is Canceled and carries the reasoning; the standing lesson
went to memory.

**FRE-1267's shipped behaviour was deliberately reversed, and that is not drift.** That ticket's AC-1
asserted the composer reaches the true screen bottom. Once it actually shipped, the owner found
edge-to-edge oversized, and rounds 11–12 put a fixed gap back (12px, then 20px). Anyone reading
FRE-1267's closed criteria against today's app will find them contradicted. It is owner-driven and
recorded on FRE-1269.

**I merged round 9 knowing codex had flagged keyboard interaction as unvalidated**, and let nine
rounds of ship-it pressure outweigh the flag. The fix was right; the gate judgment was thin. Ironically
the "regression" that seemed to vindicate the worry was not real — but the gate lesson stands on its own.

**Round 11's tests are the shape to copy.** One `CARD_GAP_PX` constant feeds both a forced-34px-inset
assertion and a zero-inset one. Same expected value under two conditions is what makes the pair
*discriminate*; a retune that pointed each test at whatever number it produced would look identically
green while testing nothing. That property survived round 12's retune — I checked, and it is the
reason both merged quickly.

## Worktrees — anything special

`build` is on `fre-1269-keyboard-regression`, which carried rounds 10, 11 and 12 and is merged. Its
local branch survived every `--delete-branch` (worktree holds it); the remote is gone. Not mine to
clean up.

## Sequence position + drift

Full session on one PWA bugfix, against the console's standing sequence (telemetry residuals →
Configuration Management → Linear async feedback → Seshat Inference, then Observability). That is
deliberate, not drift: a defect the owner was looking at every day outranks queue order, and it is
now closed. The console was not written this session and is 41/60 lines. No stream was labelled,
so nothing was dispatched behind any of it.

## Answers for the fresh start

**Why are four tickets sitting unlabelled?** FRE-1271 (watcher stale re-gate), FRE-1272 (Canceled),
FRE-1273 (Backlog — should screenshot validation become standing?), FRE-1274 (remove the diagnostic
overlay, still deployed). Approval is the owner's and none has it. Do not label them into a stream
to be helpful.

**Is the diagnostic overlay still in production?** Yes — 5 taps on the header title, or
`?debug=safearea`. It is scaffolding that outlived its purpose; FRE-1274 removes it. It is also the
only instrument here that can observe real iOS standalone behaviour, so losing it has a cost worth
naming when that ticket runs.

**Was the watcher misfiring?** Yes, and it is real: it sends `/master <PR#>` into a busy pane, the
keystrokes queue, and the invocation lands after the PR is already merged. Its own obsolescence check
runs two minutes too late. Twice observed. FRE-1271. Note the trap — the trigger ledger's
`--unconsumed` view is empty once consumed, so a watcher-sent invocation is indistinguishable from
owner-typed after the fact. I concluded "manual" from that and was wrong.

**Why is FRE-1087 suddenly the adr head?** Its due date arrived; the seam sweep activated it. Its
observation window genuinely closed 2026-08-14 (15 reset markers against a required 3), so the
picking-up session does not need to recompute or re-park it.
