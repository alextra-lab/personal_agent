<!--
TEMPLATE — the #2 session-delta artifact ("last session's bridge").

WHO WRITES IT: /prepare-reset (Step 2), on the way out, before /clear.
WHO READS IT: /prime-master (step #2), first thing, after /clear.

WHY IT EXISTS: prime-master rebuilds from DURABLE sources (memory, git, Linear,
the dispatch resolver, the owner console) and deliberately ignores prior
conversation — so the CONVERSATIONAL layer (the why, the was-doing, the
drift-and-why) is lost on /clear unless it is written down here. This file is
exactly that layer, and ONLY that layer: the overlay the live sources cannot
reconstruct. Everything else prime-master re-reads fresh — so keep this LEAN.

SIZE BOUND: 90 lines, checked at write time (ADR-0131 D5). This file is bound by
D1's rule — it may hold only what no durable source can reconstruct — because
with no plan document it is the nearest cheap surface for displaced content to
re-accrete on. Over the bound, CUT; do not spill into a new file.

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

## What was decided and why
<The reasoning, NOT the commits — prime-master reads `git log` itself and must not
be handed a second, staler account of it. Write only what no durable source can
reconstruct: a decision the diff cannot explain, an approach considered and
rejected, a correction one party made to another, an assumption now known false,
a claim that turned out wrong and what replaced it.

Test each line: if it could be derived from `git log`, Linear or a health probe,
DELETE IT. Do not list commit subjects here.>

## Worktrees — anything special
<One line per seat, ONLY if notable: priority build · preserved WIP · blocked ·
mid-something. Skip a seat that's just idle-and-clean.>

## Sequence position + drift
<Where this session sits against the owner console's standing directives and the
resolver's queue. Did we deviate or drift? WHY? Honest — drift-with-a-reason is
the point, not a confession.>

## Answers for the fresh start
<The questions the next session will actually ask, answered now. Anticipate the
re-prime's "wait, why is X like this?" and pre-empt it. Just enough.>
