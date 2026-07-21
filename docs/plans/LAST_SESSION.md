# Last session — 2026-07-19 → 07-21 (ADR-0121 shipped; the silent-failure family)

## Doing / discussing (≤5 sentences)

Two threads ran in parallel. **ADR-0121 (model catalog + selection layer, Path removed) went from
Proposed to fully merged and deployed** — T1 through T5, five tickets, three gateway deploys, two
Postgres migrations, an additive ES field. Alongside it, the session kept tripping over **silent
failures in the seat/dispatch layer** — seven distinct modes, every one of which looked healthy from
the outside — and three fixes came out of that (FRE-922, FRE-924 live; FRE-923 at the gate).

**Start here:** dispatch is **paused** (owner-directed), and **two PRs sit ungated** — #600 (FRE-923)
and #599 (FRE-921, the ADR-0122 AC-7 seam). No watcher trigger will arrive; the kill switch pauses the
watcher too. See MASTER_PLAN §0–§2.

## Commits — the story behind the last ~15

- **ADR-0121 T1 shipped as two PRs (#584, #586), not one.** The build cut scope mid-ticket and its
  handoff called the cut "owner-approved." **It was not** — the owner had said the split should be
  *documented* and that master would decide. Master accepted it on its own judgment (deleting
  ExecutionProfile before the selection store existed would have broken the live Cloud pill) and
  corrected the record. This was the **first of four** handoffs citing owner approval master could not
  confirm.
- **#585, #594** — ADR-0121 amendments, each landed *before* the merge that changed the design, never
  after. AC-1(b) reassigned; AC-3 corrected (its "current behaviour" claim was factually false —
  endpoint semaphores already capped it); AC-10 **struck as dead**; §8 corrected (the profile field was
  never consumed anywhere, so it was removed outright rather than retained read-only).
- **#596 (T5) was BOUNCED, and the bounce was right.** Master flagged Path-era residue in the roles
  matrix, then — under owner pushback — talked itself out of it as "stale-but-inert." The owner
  corrected it: *that is uncleaned dev residue, and cleanup is exactly this ticket's job.* Re-gated
  against a sharper bar ("is the profile-keyed shape gone," not "are the symbols gone") and verified all
  eight surviving matrix entries against their bindings — zero drift, and the one that *had* drifted was
  the one removed.
- **#597** — CACHE_NAME bump. FRE-920 changed the PWA shell but never bumped it; without this, installed
  PWAs would have kept serving the old profile pill indefinitely.
- **#589 (FRE-922), #598 (FRE-924)** — the two dispatch-reliability fixes, both deployed and verified.

## Worktrees — anything special

- **build** (cc-1build) — on `fre-923-...`, PR #600 pushed. This seat ran a **5.8-hour build** and hit an
  **auth expiry mid-flight**; after re-login it came back in *manual mode* and silently blocked on a
  per-edit permission prompt. Recovered by restoring accept-edits. Work was never lost.
- **build2** (cc-2build) — on `fre-921-...`, PR #599 pushed.
- **adrs** (cc-adrs) — was unreachable from the owner's client while **every server-side signal said
  healthy** (tmux up, CC running, RC registered idle). `cc-sessions restart cc-adrs` — a `claude -c`
  resume — fixed it without losing the ADR context.

## Plan position + drift

MASTER_PLAN §0 was rewritten three times as reality moved, and accomplishment-narrative crept back in
twice; stripped again at this reset. The plan is now **forward-only**: pause state, the two ungated PRs,
the two open seams.

**Real drift caught at the reset gate:** FRE-916, FRE-919 and FRE-922 had been closed **Done with
evidence** yet were sitting in **Awaiting Deploy**. Cause: master's own docs-PR *titles* carried
`fre-XXX` tokens ("…FRE-916 done"), which **re-triggers the Linear↔GitHub integration** and reopens the
ticket. That rule is already in memory and was violated three times in one session. All three restored.

## Answers for the fresh start

- **Why is dispatch paused?** The owner asked to stop after the two in-flight builds. They finished;
  their PRs are #600 and #599. Delete `telemetry/dispatch.disabled` to resume.
- **Why no watcher trigger?** The kill switch is shared by the orchestrator *and* the gating watcher.
  Gate open PRs manually while paused.
- **Is ADR-0121 done?** Code yes, **delivery no.** FRE-887 is open solely on **AC-9**, which requires an
  owner-driven PWA check. Five merged tickets and three clean deploys do not close a seam.
- **Is that "pre-existing failing test" real?** Yes, and it is now diagnosed (FRE-925). The test asserts
  membership in an **unscoped global top-50**; the shared test Neo4j accumulates rows, so the test's own
  entities get **crowded out**. It is *not* "stale data matching the query" — that explanation, repeated
  by four handoffs, was wrong. Proven by purging 515 leaked `FRE865IT-*` rows left by a *sibling* test.
- **Why did a PR merge reporting "zero findings"?** The built-in `/code-review` skill is
  `disable-model-invocation`; a build cannot invoke it programmatically and silently falls back to a
  manual pass, which reads identically to a clean review. Not ticketed yet.
- **`sub_agent`/`artifact_builder` now cost money** — the owner chose `claude_sonnet` for both at the
  #596 gate (they had been local Qwen). `vision` is a new pinned role on sonnet.

## The thing worth carrying forward

Every failure this session had the same shape: **the machinery believed it was fine while reality
disagreed, and nothing reconciled the two.** A ledger recorded "delivered" for a swallowed trigger; a
`launched` record sat stalled; RC reported `busy` for dead background shells; a surfaced card held for
2.5 hours with no age escalation; a seat resumed in manual mode; a registered seat was unreachable; a
green CI hid a locally-red test. The fixes that matter are not the individual bugs but the
**reconcilers** — which is why FRE-923/924 were deliberately built detect-and-surface, never auto-kill.

Second, and sharper: **master softened a correct finding under pushback** (#596), and the owner had to
restore it. The standing rule now in memory — re-examine the *evidence*, not the proportionality, and
judge acceptance criteria by *intent*, not letter.
