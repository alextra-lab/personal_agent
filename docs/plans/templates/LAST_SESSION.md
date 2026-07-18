<!--
TEMPLATE — the #2 session-delta artifact ("last session's bridge").

WHO WRITES IT: /prepare-reset (Step 2), on the way out, before /clear.
WHO READS IT: /prime-master (step #2), first thing, after /clear.

WHY IT EXISTS: prime-master rebuilds from DURABLE sources (memory, git, Linear,
MASTER_PLAN) and deliberately ignores prior conversation — so the CONVERSATIONAL
layer (the why, the was-doing, the drift-and-why) is lost on /clear unless it is
written down here. This file is exactly that layer, and ONLY that layer: the
overlay the live sources cannot reconstruct. Everything else prime-master
re-reads fresh — so keep this LEAN. Set just enough context, no data dump.

IT IS THE REVERSE OF prime-master: it writes the overlay for what prime-master
reads (current state + target); the process (#9) is static and needs no overlay.

ROLLING: overwrite this file each reset — it is always "the LAST session."
There is no history file (deleted 2026-07-18 as write-only overhead). What
shipped lives in the git log; why a decision was made lives on the Linear
ticket. Do not archive this file's content anywhere — it is superseded, not kept.

This is a TEMPLATE: adjust the sections as we learn what's working and what
isn't. Copy it to docs/plans/LAST_SESSION.md and fill it in.
-->

# Last session — <YYYY-MM-DD>

## Doing / discussing  (≤5 sentences)
<Exactly what was in flight at the reset — the thread to pick up. No history,
just "you were here, mid-this." Five sentences or fewer.>

## Commits — the story behind the last 10
<For the last ~10 commits: not just what git shows, but the OUTSIDE FACTORS from
the conversation the commit messages don't carry — the reasoning, the thing we
tried and rejected, the "because X". git has the what; this has the why.>

## Worktrees — anything special
<One line per seat, ONLY if notable: priority build · preserved WIP · blocked ·
mid-something. Skip a seat that's just idle-and-clean.>

## Plan position + drift
<Where this session sits against MASTER_PLAN (the target). Did we deviate or
drift from it? WHY? Honest — drift-with-a-reason is the point, not a confession.>

## Answers for the fresh start
<The questions the next session will actually ask, answered now. Anticipate the
re-prime's "wait, why is X like this?" and pre-empt it. Just enough.>
