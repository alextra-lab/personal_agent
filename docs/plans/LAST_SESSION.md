# Last session — 2026-08-05 (the day the owner cut scope, and master kept verifying the wrong artifact)

## Doing / discussing

The owner ended the pause by naming **VPS/Cloud Architecture Stabilization + ADR-0132** as the subject,
and that project is now nearly closed out. The session ended clean at a deliberate boundary: nothing in
flight, both build seats idle, no open PRs. The one live thread is **FRE-241** — the slm_server watchdog,
rewritten today and now carrying its own working instructions because that repo has no machinery to
enforce anything. It is **owner-dispatched**; no seat here can reach that repo. One question is open and
unanswered: whether to give slm_server minimal CI first.

## What was decided and why

**The uptime figure cannot be cited as evidence that availability is fine, and this is the most important
thing to carry forward.** The gateway's SLM health probe hits the router's `/health`, which returns a
hardcoded string and whose own docstring calls it a check *for the router itself*. So the 30-day figure
(98.39% up, 8,569 samples) measures **router** availability. A wedged model is recorded as **up** —
structurally invisible, not merely underrepresented. Master quoted that figure as decisive before
discovering this, and the owner's "how you determine *usable* is critical" is what forced the check.

**"Usable" is determined by monitoring errors on real traffic** — the owner's call, and better than the
synthetic-probe design master proposed. A probe occupies the model every cycle, and a small probe prompt
can pass while a real long-context turn wedges. Real errors *are* the definition. Known caveat, stated
rather than designed around: error-monitoring is traffic-dependent, so an idle wedge isn't caught until
someone hits it.

**Three slm_server tickets collapsed to one, and the reasoning is not obvious from the outcome.**
All 138 down-samples in 30 days were `reachable=false` — none reachable-but-degraded — so FRE-444's
enriched health fields described a failure mode this system does not exhibit. The owner's independent
point (single-user MBP, no concurrency, so queue-depth and GPU-util carry no decision) reached the same
place from the hardware. FRE-238 died with it. Note the ordering: master had *sequenced* 444→241→238
before asking whether any of it was worth wanting — the owner's "do we really give a shit about 444?"
is what surfaced that.

**Master's dominant failure today, twice, both owner-caught: verifying a plausible *adjacent* artifact
instead of the file the ticket names, then reading the match as confirmation.** FRE-338 names
`docker/mcp/run-gateway.sh`; master checked a settings field. FRE-340 names
`infrastructure/scripts/transfer-models.sh`; master searched `scripts/`. Both were wrongly cancelled and
had to be reopened. Each ticket states its path — one command each would have returned the opposite
answer. It nearly happened a third time: `heartbeat` *is* in slm_server, as SSE keep-alive, which a
keyword search would have read as satisfying FRE-241.

**An ADR's body can lag its own amendments — read Status Updates before concluding a constraint is open.**
Master told the owner ADR-0105's dead-embedder reference needed a design judgement. It did not: the ADR's
own 2026-07-06 amendment (via ADR-0112) had already permitted a managed endpoint a month earlier. The body
had simply never been reconciled. The part that mattered: **AC-10 required a run to succeed "using only
`embeddings:8503`"** — an acceptance criterion that could not pass, which nothing would have surfaced until
someone tried to adjudicate it.

**The `embeddings` drop survived three owner challenges, each strengthening it** (full analysis on FRE-597).
Worth carrying only as a pattern: the owner's "some ticket or ADR must have specified creating it" was the
question that settled it — git said nothing did.

**FRE-619's second attempt was worse than the original defect** — the guard got selected but still skipped,
so a green *required check* asserted coverage that didn't exist, where before it plainly looked unwired.

**The theme, four separate instances in one day:** mechanisms reporting success while checking nothing —
FRE-619's silent skip, CI's schema-apply swallowing four errors and exiting zero, the stale health-URL
override, ADR-0105's unsatisfiable AC. Three were visible only by reading actual output rather than status.

## Worktrees — anything special

- **build1 wedged on an interactive permission prompt** — third time in two days. Master released it with
  a send-keys `1`. It is invisible in dispatch state, so nothing detects it but a human looking.
- **A build seat's commit appeared on master's local `main`** (FRE-619's, while its PR was open). Only
  `git pull --ff-only` caught it; branch protection was the second layer. Concurrent-worktree hazard.

## Sequence position + drift

The owner drove the whole day and named the subject explicitly, so this was directed work, not drift.
**The console's standing four-item sequence (telemetry residuals → Config Management → Linear async
feedback → Seshat Inference) remains unstarted** — unchanged from yesterday. Both console directives
touched today were transcribe-then-retire, both conditions met and cited.

## Answers for the fresh start

- **Why does FRE-241 carry unusually long working instructions?** Because slm_server has *no* machinery —
  verified: no `.claude/`, no CI, no lifecycle rules, `main` directly pushable, and its CLAUDE.md mentions
  Linear/tickets/gates/PRs zero times. The ticket has to be the gate.
- **Why is FRE-1163 High and in Needs Approval?** Three occurrences today. The third proved it can swallow a
  *safety* signal: the watcher correctly re-triggered on a changed head to say "your review is stale," hit a
  busy seat, and the message was lost — which is exactly when master merged the stale head and the owner had
  to decline it.
- **Is the local-model residue finished?** No. FRE-1165 (live system prompt still advertises the dead
  containers) and FRE-1166 (script, mount, compose definitions) are open. FRE-1125 and the ADR-0105 site are
  done.
- **Was anything deployed?** Yes — the ADR-0132 chain earlier, and the `embeddings` DROP against prod
  (owner-authorized, zero rows, DDL captured to `telemetry/purge-backups/` first).
- **Open, undecided:** minimal CI for slm_server so its existing pytest suite runs on push. Master raised it
  twice; the owner has not answered. Not filed.
