# Last session — 2026-07-30 (the day entity recall was actually solved, and not by master)

## READ THIS FIRST

- **Environment is UP and fully deployed.** Gateway image 15:26Z, PWA 17:07Z (`seshat-v42`), ES template
  registered, watcher restarted onto FRE-867's detector. Nothing merged is undeployed.
- **The board has no open exceptions** — no `Verify Failed`, nothing half-closed.
- **Do not re-open the entity-recall mechanism.** It is fixed and verified live. Five explanations
  preceded the real one; the history is on FRE-1041/1060/1061/1062. See §0b of MASTER_PLAN for what
  remains genuinely *unmeasured*.

## Doing / discussing (≤5 sentences)

The session began on process work and ended having solved a five-day problem: entities never reached the
model because the proactive path retrieved an *(entity, turn)* pair and the payload builder discarded the
entity whenever the turn had text. Master proposed two wrong mechanisms during the day and was corrected
both times; the owner eventually routed the problem to Fable in the explore seat, which measured the
graph, found the flattening plus a second dedupe defect, and shipped both halves. Everything else —
FRE-867's watcher, FRE-927's seat counter, FRE-1051's delivery probe, FRE-937's turn summary, FRE-1042's
drawer — landed alongside it.

## What was decided and why

- **Master may approve read-only permission prompts itself; anything mutating goes to the owner.**
  PR #755 shipped a rule routing *every* prompt to the owner, which converts an 18-hour silent hang into
  an owner-latency hang. Settled on today's own evidence: of five prompts master answered, four were
  read-only SELECTs it should answer and one was an `rm` it should have escalated. Corrected in #757.
  **The watcher's nudge text still carries the superseded "never approve yourself" wording** — it also
  produced one false positive at 17:33 (stale snapshot; seat had already moved on). Fold both into
  whatever next touches `gating_watcher.py`; not ticketed.
- **`prepare-reset` owns unwritten context; `prime-master` owns `git log`.** Owner's call, PR #766. Both
  skills were narrating the same commits and prime was reconciling two accounts of one thing. This file
  now carries reasoning only — if a line here could be derived from `git log`, Linear or a probe, it
  should be deleted.
- **The `/code-review` skill is `disable-model-invocation` and no seat can call it.** It blocked four
  seats today. Master accepts the adversarial-agent substitute at the gate, provided the handoff names
  what ran instead. This still has no permanent resolution and will recur.
- **CI polling is banned by *behaviour*, not by tool** (#761) — a seat read "never arm a `/loop`" and
  spawned a Monitor instead. The reason is **redundancy** (the watcher covers it), *not* prompt-cache
  cost; that rationale only holds for `/loop` and overstating it is how a rule gets argued away later.
- **FRE-1057's severity was settled and is lower than feared.** The study wrote 430 rows to the
  production ledger and *read* 136 real user sessions, but did **not** write the knowledge graph — the
  matched Session nodes predate the study by six weeks. Remedy is a ledger question, not a graph one.

## Worktrees — anything special

- **explore** — Fable worked FRE-1061/1062 here and its worktree still holds those branches, which is why
  `--delete-branch` failed on merge. Harmless.
- **PR #738 (FRE-1015) is DRAFT, CONFLICTING, 46 commits behind.** It needs *re-baselining, not merging*.
  Its acceptance precondition was literally unsatisfiable until today's fixes; that is why its tests were
  tautological and why nobody could write the honest version.
- **`master-914`** — still stale on the closed `fre-909-seat-rename`. Harmless.

## Plan position + drift

MASTER_PLAN header compacted 74 → 49 lines this reset; the resolved entity narrative was deleted rather
than relocated, and its standing lessons went to memory
(`feedback_measure_dont_infer_and_check_the_answer`). §0b now holds only what is genuinely unmeasured.

We deviated from the primary streams on 2026-07-25 when session summarisation blew up, and returned today:
ADR-0123 is one owner-turn from closing and ADR-0126's chain is unblocked.

## Answers for the fresh start

- **Why was master wrong so often here?** It inferred mechanisms from telemetry instead of reading the
  retrieval path and counting the substrate. The fix came from `7,442 of 7,446` — a count, not a theory.
  It also verified at the wrong altitude: it read a recall record, saw the entity absent, and reported a
  *correct, personalised* answer as a failure.
- **Why does `Awaiting Deploy` mislead?** It means both "not deployed" and "deployed, unverified".
  **FRE-1059** (Needs Approval) fixes it. This bit three times today, including master's own status
  report and `prepare-reset`'s Step 1 safety gate, **which is unsatisfiable as literally written** —
  16 tickets sit in that column. Read it as "nothing mid-flight through me", not literally.
- **`prepare-reset` does not check health, actuation or the trigger ledger** — `prime-master` does. It can
  therefore bless a reset into a dead watcher. Worth folding into FRE-1059's work.
- **CI cannot see WebKit-only layout defects.** FRE-1042's row-height bug survived a fully green run
  because it does not reproduce in Chromium, and the PWA suite is jsdom + Chromium only. Not ticketed.
- **The adrs seat found something above its ticket:** ADR-0128 governs what fields are *called* but has no
  rule for what *earns a place* in the index. The owner supplied the missing principle — a record exists
  because it answers a named question. If that seat proposes amending the ADR rather than delivering
  FRE-1043, that is a scope decision for the owner.
- **Awaiting owner approval:** FRE-1044–1050 · FRE-1051's follow-ups (FRE-1055/1056/1057/1058) · FRE-1053
  (re-scope small) · FRE-1059 · the FRE-1011 rescope.
