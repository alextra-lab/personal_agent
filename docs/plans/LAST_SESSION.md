# Last session — 2026-07-26 (harness restarted; the digest thread went four tickets deep)

## READ THIS FIRST — the environment is UP again, deliberately, with the background streams OFF

**The gateway was restarted at 18:10Z (owner-authorised) and is healthy. The dispatch kill switch is
LIFTED. Six background LLM streams remain disabled in `.env` and must stay that way.**

- `cloud-sim-seshat-gateway` running at `5b0675a5`. `/health` green on all five components.
- **Disabled in `.env`, verified in the running container's env:** `AGENT_SESSION_SUMMARY_ENABLED`,
  `AGENT_INSIGHTS_ENABLED`, `AGENT_INSIGHTS_WIRING_ENABLED`, `AGENT_FEEDBACK_POLLING_ENABLED`,
  `AGENT_REFLECTION_RECALL_ENABLED` — all `false`. Reflection additionally throttled to a 1000-day
  interval (**no real off-switch exists — FRE-990**).
- **All Session nodes marked non-eligible for the digest sweep.** Even with the flag flipped on and the
  code unchanged, the eligibility query returns zero rows. Belt and braces, deliberate.
- `telemetry/dispatch.disabled` removed (kept as `.lifted-20260726`). Dispatch daemon + watcher live.
- `cloud-sim-embeddings` re-stopped after the rebuild, per the standing rule.

**Do NOT re-enable the summary sweep.** Both the build session and master concluded independently: it
would store *fewer* usable digests than today while looking healthier in the logs. Calibrate the bound
(FRE-994) and bound the retry path (FRE-987) first, then gate on the **empty-digest rate**, not the
parse rate.

## Doing / discussing (≤5 sentences)

The session began with the harness stopped after yesterday's cost incident and ended with it running
again, five PRs merged, and the digest subsystem diagnosed far more deeply than expected. The owner's
question "why is free-form output used for data that must fit a KG-node schema?" opened the thread that
produced FRE-995 (audit), FRE-996 (contract pilot, real spend), FRE-997 (KG-write-path signal) and
FRE-998 (the graph holds no user identity at all). A one-line test — *"do you see any captains_log cost
today?"* — produced **four independent cost-reporting defects**, all on the read side, all now on
FRE-989. Substantial data work landed too: the capture corpus was cleaned of 927 test records and 242
real April captures restored, and 118 graph sessions were backfilled with their true owner. The
summarizer redesign brainstorm is scoped and briefed but **has not happened yet**.

## Commits — the story behind the last 10

- **PR #682 → FRE-995** — the structured-output audit. Twelve call sites inventoried; **the capability
  was already wired and essentially unused**. It falsified three premises master had written into the
  ticket, and master verified all three against source before accepting them.
- **PR #684 → FRE-997** — entity-extraction fail-open signal. Codex plan-review caught a *fatal*
  finding pre-implementation. Cleanest gate of the day.
- **PR #683 → FRE-996** — the JSON contract pilot, three revisions deep, and the most instructive work.
  Real spend ($1.21 vs a $2.33 estimate, **study lane**). Its three docs commits are all self-corrections:
  a litellm check against 1.93.0, an end-to-end delivery report that *did not favour its own change*, and
  a withdrawal of that regression once the owner showed the metric was circular.
- **Two outside factors the messages don't carry.** (1) Master **wrongly bounced #683** for a missing
  codex review — codex *had* run, recorded in the plan doc, which master never opened. Bounce withdrawn,
  build told not to redo anything. (2) The owner caught that "over budget" was scored against the
  uncalibrated 250-token placeholder — making the pilot's headline metric circular. That single
  observation reshaped both #683 and FRE-994.

## Worktrees — anything special

- **build2 — IN FLIGHT on FRE-994** (In Progress since 17:58), branch `fre-994-digest-compression-curve`.
  This is the compression curve and **it spends money**. It resumes its own context on wake.
- **build1** — on `fre-996-digest-json-contract`, work finished and merged; idle.
- **adrs — BLOCKED on a modal dialog** ("Enter to confirm"). No work assigned; needs a keypress or reset.
- **explore** — on `docs/session-analyzer-pillar` (merged as #679).
- **`master-914`** — stale worktree on `fre-909-seat-rename`; FRE-909 is closed. It is the only reason
  that branch survives. Removing it was offered and not taken.

## Plan position + drift

MASTER_PLAN was rewritten this session and is current. The audit project is the live thread; §0's
"environment is down" block is replaced by the restarted-with-streams-off reality.

**Deliberate deviation:** master sequenced **FRE-994 ahead of the summarizer brainstorm**, despite
having recorded the opposite advice on FRE-993 earlier the same day. Reasoning is on the FRE-994 ticket:
the curve produces exactly the evidence the redesign needs, and running it does not commit to keeping
the digest. The owner did not object. If the next session disagrees, the brainstorm brief (`0e715eac`)
is the input and FRE-994 can be parked again — **label and relations together this time.**

## Answers for the fresh start

- **Do NOT re-enable the summary sweep, and do NOT raise any budget cap.** Both standing.
- **The threshold stays at 250.** The owner explicitly declined a change to 500 pending FRE-994.
- **Cost questions must be answered from Postgres `api_costs`**, never Elasticsearch — the ES cost event
  carries no `purpose`/`role`, so role-attributed spend is unanswerable there *by construction*. Three
  further read-path defects are on FRE-989.
- **Awaiting Deploy is 12 tickets and cannot be cleared today.** FRE-996/997 are deployed but their
  acceptance needs traffic (997) or the sweep enabled (996); the other ten predate this session. Master
  deliberately did not close any on "deployed and healthy" — that is the artifact-level assertion the
  gate exists to prevent.
- **The brainstorm has not happened.** Master said it would drive it in `cc-adrs`; that seat is currently
  stuck on a dialog. Brief is on main at `0e715eac`.
- **FRE-937 carries a reversed decision.** The owner now wants the turn-progress surface to *fade* after
  the response completes; the ticket currently argues collapse-not-vanish, justified by the owner's own
  earlier question. Recorded as a reversal so a future reader doesn't undo it. Two live defects are on it
  too: the tools counter is blank despite 8 tool calls, and "Writing the response" ×4 is correct (one per
  tool round) but reads as a stutter.
- **Modal dialogs swallowed dispatches four times today** (resume, redirect, model-switch, adrs). Each
  needed a human to notice. That is FRE-976's subject and it is still parked.
- **A recurring master failure worth knowing about:** five times today master read a *proxy* and treated
  its silence as the answer — a last-write-wins reason field, a session-id shape, `_cat/indices` counts, a
  grep of the PR body for "codex", and a `None`-to-zero coercion. The owner caught most of them. When a
  conclusion turns on whether something exists, open the artifact that would record it.
