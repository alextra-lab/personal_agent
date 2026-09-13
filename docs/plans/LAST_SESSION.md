# Last session — 2026-09-13

## Doing / discussing  (≤5 sentences)
The session ended at a **planned VPS reboot** (kernel 6.12.95 → 6.12.107, `reboot-required` set since
2026-08-31, uptime 9 weeks). `cc-master` is itself a tmux seat, so this file is the only thing that
crosses the boundary. Four tickets moved: FRE-1500 and FRE-1499 are Done and live-verified; FRE-1503
and FRE-1325 are merged with **FRE-1325's gateway rebuild deliberately not run**. The owner is at the
keyboard, the local SLM stays offline, and work runs on OVH by their direction.

## The first thing to do after the reboot

**FRE-1325 is merged and NOT deployed.** `ENV=cloud make rebuild SERVICE=seshat-gateway`, then verify
the disclosure line on a live turn before moving it to Done. The deploy was held because a rebuild
minutes before a reboot is wasted, **and** because the owner was given a veto on the feature and had
not answered. Ask before deploying if they still have not.

Everything else recovers itself: all thirteen `cloud-sim-*` containers are `unless-stopped`, and
docker, both dispatch daemons and `seshat-soak.timer` are `enabled`.

**The seats do not.** `cc-sessions` recreates them, and each lands on a blocking *"Resume from
summary"* prompt. Nothing will flag that — see FRE-1504 below. Answer all five by hand.

## What was decided and why

**`enforce` is not the destination — the turn ships and states what it could not ground.** Owner's
decision, relayed onto FRE-1328. Master's supporting analysis matters more than the decision: master
tested it against FRE-1327 and found **declaration does not detect fabrication**. That turn's four bash
calls were refused as inadmissible, so it would truthfully declare "I worked from commands the contract
cannot cite" — and the figures were still invented. An honest uncitable report and a fabricated one
produce the identical line. So **ADR-0140 Option 6 / FRE-1361 stays open**, against the previous adr
session's untested guess that it might become unnecessary. `enforce` narrows to the population where
retrieval can help (nothing offered, nothing used) rather than dying.

**FRE-1325 shipped the surface, and it fires on ~92% of turns.** Master measured rather than trusting
the ticket: on the twelve most recent grounding records the note would fire on **eleven**, one reading
`41 of 41`. AC-2 ("a fully-compliant turn shows nothing") passes on its letter while the indicator is
effectively a badge, because fully-compliant turns barely exist. **That figure is FRE-1328's ADR
input** — the two-shape discrimination (structural refusal vs nothing-offered) is that ADR's to decide,
and the build seat was right not to reach for it.

**No OVH re-run of the FRE-1498 arm.** Master proposed it claiming it would close the study's
answer-quality gap. Wrong: that harness has **no rubric and no scoring** — "quality" appears once in
the whole document, inside a quoted prompt. Scored quality lives on FRE-1495. Owner: *"No need to waste
time or resources."*

**FRE-1487 was NOT closed, against master's own recommendation.** Master advised closing it to free
FRE-1495, the owner approved, and master then read the thread and found the premise false — the series
never re-ran (AC-2 partial, AC-3 not scored, AC-5 failed on `channel=CHAT`). The real fault was a
**circular dependency**: FRE-1495 was blocked-by FRE-1487 while FRE-1487's own comment waits on the
chain ending in FRE-1495. Broken by removing FRE-1495's relation — it needs that study's *query set and
criterion*, which exist as text, not its results.

**FRE-1495 is local-dialect and parked.** Master over-read "rescoping to ovh is ok" as moving the whole
study to OVH; the owner corrected it. The question is whether **llama.cpp's grammar** costs answer
quality, and OVH is a different constrained-decoding implementation, so it cannot answer it. Reverted.
`qwen3.8-flash-next` is not under review and stays the bound local primary.

**Four master errors worth not repeating.** Claimed FRE-1487 "delivered" without reading its thread ·
claimed `telemetry/` was gitignored when only subpaths are · filed FRE-1499's Fault B on an inference
the transcripts refuted · said the FRE-1498 harness existed only in `/tmp` when the explore seat had
already copied it to `~/fre1498-harness` in the owner's home at 09:07. The owner caught three of the four.

## Worktrees — anything special
All four are on merged or stale branches and idle. The four untracked `*.bak-*` files at root are the
owner's — do not remove them.

## Sequence position + drift
Dispatch is paused by kill switch for the reboot only. `rm telemetry/dispatch.disabled` to resume.
Both build streams are idle with **nothing queued**: FRE-1495 and FRE-1501 are parked on the offline
local backend, each with the reason recorded on the ticket so neither reads as an oversight.

The raw 400 result rows behind the FRE-1498 document are still **outside git**, at
`~/fre1498-harness/out/` in the owner's home (survives the reboot — disk, not tmpfs). The explore seat asked for
them under `telemetry/evaluation/fre1498/` on the FRE-1337 precedent. The regime patches and compose
overrides are likewise uncommitted. Surfaced on FRE-1503, not filed.

## Answers for the fresh start

- **What is at the gate?** Nothing. Zero open PRs at the reboot.
- **What is the live risk?** FRE-1504 (Needs Approval, Urgent): FRE-1457's held-prompt wedge detector
  is **silent in production**. 77 seat-busy ticks, 0 wedge events, empty wedge state — the same
  measurement as FRE-1457's own, which is Done. Every component passes in isolation; the composition
  does not. It cost 3 hours today on both build streams at once, and it is why the post-reboot resume
  prompts must be answered by hand rather than waited on.
- **Is the adr seat stuck?** No. FRE-1328 holds the stream; the owner's decision is on the ticket and
  the ADR can be written now on choice-axis evidence. FRE-1502 (planner decides expansion) is Approved
  behind it, and its stated gate on a quality comparison **will never be satisfied** — that is recorded
  on FRE-1502 so the seat does not wait for it.
- **What needs the owner?** FRE-1504's approval · whether FRE-1325 deploys at all · FRE-1487's AC-5
  (the eval channel does not gate KG writes) before that study re-fires.
