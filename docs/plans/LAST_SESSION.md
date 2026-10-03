# Last session — master, 2026-10-03 08:00 UTC to 14:20 UTC

## Doing / discussing  (≤5 sentences)
The owner asked for "all recommendations" to be followed; cc-master was then restarted (`-c`).
FRE-1540 merged and its daemons restarted; its AC-4 waits for a natural alert. The owner
accepted ADR-0154 and approved its chain at 13:01: the build streams now run it (FRE-1537 holds
the record). FRE-1512 deployed at 14:15; its AC-2/AC-3 live halves need the first 20 turns.
FRE-1511 runs on build2. FRE-1543 queues on build1 after FRE-1471 (owner: "leave order as is").
The Awaiting Deploy sweep closed six tickets. The rest wait on the owner decisions listed below.

## What was decided and why

**ES retention: a monthly index keeps the current month plus 3 full months (owner rule).** ILM has
no month unit, so delete `min_age` is 123d (July to October). The guard test enforces it. Three
`*-2026-07-05` indices had entered the delete phase before the policy PUT, and master moved them
back with `_ilm/move`. A policy change does not stop a delete in progress.

**Correction: September logs are not lost.** Master told the owner they were. Only the ES copy is
gone. The gateway's disk log (`current.jsonl*` in the telemetry volume) covers 2026-07-30 onward.

**Status bar (owner):** tools reset at the next send. ctx resets only at compaction or a new
session ("I need to know how much headroom I have"). This replaces FRE-1401's rule that ctx is
never restored, for the same session only. The cross-session guard stays.

**Approval on production harness turns: no bypass (owner).** A harness turn has no PWA socket, so
an approval tool is denied. FRE-1539 marks such a turn invalid. Tools that need approval run on the
eval stack (opt-out plus `bash` as `nobody`). Master's first FRE-1539 probe used `echo`, which is on
the bash auto-approve list and ran. Use a command outside that list, such as `pwd`.

**`AGENT_ENABLE_SECOND_BRAIN=false` does pause consolidation.** The event consumer stays
subscribed, but `scheduler.on_request_captured` checks the flag first (`scheduler.py:348`).
`/health` still reports `second_brain: running`. That shows only that the scheduler is up, so prove
the pause with the graph count.

**The dispatch channel never worked until today.** Seats started by `cc-sessions` lacked the port,
the secret and `--channels`, so every trigger fell back to `send-keys`. Fixed in `~/cc-env`
(commit 5fff3b4, port column plus `cc-seat-exec`), and all three worker seats were restarted.
cc-master has no channel by design. The owner said: "Fix the discovered root cause".

**A stale draft in a seat's input blocks the watcher.** cc-1build held an old unsent "Master
bounced PR #1161" text. The watcher read the seat as busy for 2.5 h. FRE-1540 makes such a stall
reach the owner.

**The 09:01 searxng outage repeated FRE-1344**, whose `required: false` fix did not hold.
FRE-1542 (Done) made `make eval-infra-up/down` safe: eval services only, `--no-deps`, a pinned
project name and a dry-run plan guard. A hand-typed `docker compose -p seshat … up` is still unsafe.

**FRE-1538's tools half failed live; the server option replaces it (owner: "yes").** On return
after several app switches, ctx showed 19K (correct) but tools showed —/— (should be 2/25). The
folded call-history panel was also gone: it is built in page memory at DONE and was never stored on
the server, so any iPadOS reload loses it — this predates FRE-1538. FRE-1543 stores the call
history with the message and sends a status snapshot on connect. FRE-1538 closed on its ctx half.

**`first_token_ms` is the time to the whole reply.** Nothing streams token by token today, so
FRE-1512's field is not a true first token. The FRE-1515 baseline still compares like with like.
FRE-1515 may not merge before 2026-10-17 14:15 UTC (14 days, plus 20 qualifying turns).

## Worktrees — anything special
- The eval stack is up (gateways :9002 and :9003, substrates `*-eval`) under project `seshat`.
  Start and stop it only with the make targets. The test stack (`seshat-*-test-1`) shows as orphans
  of that project, so never pass `--remove-orphans`.
- Untracked `*.bak*` files are the owner's.

## Answers for the fresh start
- **State:** use `next_resolver`, Linear and `telemetry/dispatch_state.json`. None of it is copied here.
- **Owner decisions pending** (each recorded on its ticket by the sweep, or still to be asked):
  FRE-1473, confirm the 30-day and 10-item ceilings · FRE-1477, pick a course (reranker bound,
  another embedder, or a lower 90% bar) · FRE-1359, accept window 1 read from the disk log as the
  AC-3 source · FRE-1398, retitle or close in favour of the slm_server stderr-pipe defect ·
  FRE-1122, authorize the baseline run · FRE-1402, three owner turns (confirm the skill really
  loads: `skills_loaded` has never shown `sequential-thinking`) · FRE-1514, approved but
  unlabeled: its criteria cite ADR-0152 D6 and call the measure open; the owner or the adr seat
  must restate them against ADR-0154 D7 before it can be queued.
- **Also open:** FRE-1485 (AC-2 and AC-3), FRE-1527 (AC-5) and FRE-1382 (AC-1) need a natural or
  authorized live turn. FRE-1474 needs its generation check turned on, in an SLM window on the
  owner's Mac. FRE-1503 needs an eval run. FRE-1372 waits on FRE-1506.
- **Forced-synthesis overflow (FRE-1485 finding):** on 91b57b5c, keeping 25 tools in the forced
  synthesis pushed a 120k prompt past the window. Not ticketed yet.
- **cc-explore is the owner's seat.** Do not message it. Master restarted it at 13:43 on the
  owner's word ("restart it"), with context kept. All five seats now run Claude Code 2.1.288.
- **A seat restarted outside `cc-sessions` loses the channel.** Check with `ss -ltn | grep 879`.
