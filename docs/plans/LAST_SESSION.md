# Last session — 2026-08-11/12 (five merges, and master's own instrument wrong three times)

## Doing / discussing

Both build streams are mid-ticket and healthy: build1 on FRE-1219 (retiring four telemetry spellings at
their emit sites), build2 on FRE-1211 (the eight-dashboard Postgres fan-out, batch 1 of 3 done). Nothing
sits at the gate. The Kibana retirement chain is one step from complete — T7 landed, and T9 (FRE-1214) is
fully prepped and deliberately **not** dispatched, which is the thing most likely to be undone by mistake.

## What was decided and why

**Master's own measurements were wrong three times in one day, and the pattern matters more than any of
them.** Each time the shape was identical: I sampled something narrower than the claim I then made.
I told a build there were zero divergent `cost_live_usd` rows — there were 457, and the whole AC-4
workaround was built on my error. I called FRE-1186's false-red "latent" after sampling two traces; it
was actively firing. I recommended an ultra review off a 3,525-line headline that was 1,217 lines of
generated JSON and 882 of tests — 588 lines of actual src. **All three were caught by re-measuring, none
by intuition.** Suspect the instrument before the result; the owner's challenges are what forced two of
these re-measurements, so treat a challenge as a prompt to measure rather than to defend.

**Escalated-review calls should be sized by measured src surface, not by diff headline or by the word
"migration".** FRE-1209's escalation was waived because the table had no reader; FRE-1210's was waived
because the migration was purely additive and the risky part (Cypher) had already been codex-reviewed,
cross-checked at 25/25 cells and covered by six live-Neo4j tests. Both waivers are recorded on their
tickets with reasoning — do not re-litigate them, but do reuse the method.

**Two distinct causes produced five seat strandings; conflating them would fix neither.** One is the
Claude update flipping `defaultMode` to `auto`, whose classifier is unreachable here so bash is denied
outright. The other is genuinely subtle: seats append `2>&1` to psql calls, and the FRE-867 allow-hook
deliberately refuses to auto-allow anything carrying a redirection. The hook is working as designed —
the fix is behavioural (stop redirecting psql output), not a hook change, and widening the allowlist
would remove a guard doing real security work.

**A worktree seat runs its own branch's copy of a hook, not main's.** So a hook fix does not reach
in-flight seats until they rebase. Harmless this time because the logic was identical.

**"Approved" does not mean dispatch-ready, and this is why feeding a stream is not just labelling.**
FRE-1222 has *no acceptance criteria at all* plus two undispositioned open remedies, one of which is an
unadjudicated design choice — labelling it would have guaranteed a bounce. FRE-1219 also had none; its
scope was unambiguous enough that I wrote four decidable criteria rather than punting. Expect more of
these among the Observability Approved set.

**Two criteria were rewritten at dispatch because they required an owner action** (ADR-0130 D6):
FRE-1210's AC-2 wanted entities "the owner recognises", FRE-1214's AC-4 wanted the owner to confirm
interactively. Both now have mechanical equivalents that protect the same property. This check keeps
catching things — run it before every label.

**A false premise propagated into three tickets and nine repo assertions**: that `request_trace` died
2026-06-07. It is live (252 docs, last 2026-08-08). Corrected in FRE-1212 and FRE-1211 before either
built; FRE-1211 fans out to eight subagents, so the wrong premise would have multiplied by eight. The
plan doc and the panel descriptions still carry it.

## Worktrees — anything special

**build1** was dead for ~15 hours on an auth 403 ("Please run /login"), not a stall — seat-specific, no
other seat affected. Restarted, resumed **from summary**, mode cycled back to accept-edits, and re-briefed
on its own pre-crash findings. Its investigation lives in that summary, not in the branch, which is empty.
**The primary checkout at /opt/seshat was moved onto a feature branch twice by build seats.** That is a
real deploy hazard, not just untidiness: `make rebuild` builds from the working tree, so deploying while
it sits on someone's branch ships the wrong code. Check `git branch --show-current` before any rebuild.

## Sequence position + drift

On the console's Observability directive throughout; no console write this session, and it sits at 41 of
its 60-line bound. One deliberate deviation, owner-directed: FRE-1210's AC-6 and its two sysgraph panels
were split to FRE-1248 rather than bouncing a 3.5k-line PR, and AC-6 was struck from FRE-1210 openly so
the ticket did not close carrying an unproven criterion.

## Answers for the fresh start

- **Why is FRE-1214 Approved, unblocked, prepped — and unlabelled?** Deliberate. The owner's directive
  reserves the Kibana retirement declaration to them ("I will tell you when kibana is retired"). AC-4 is
  rewritten and FRE-1246 is folded in as work item 9. It needs one word from the owner, not a fix.
- **Why does FRE-1223 sit In Progress since 2026-08-10?** Mac-side bookkeeping lag. Its chain is proven
  end to end by FRE-1230's procedure. I offered to close it on that evidence; the owner has not answered.
- **Why is FRE-1245 still Needs Approval when it cost three stalls today?** It has not been approved.
  41h, 4h and 15h, three different causes, one identical detection failure: a ticket reading In Progress
  makes the stall check structurally unreachable. Every hour it waits costs roughly a stream-day.
- **Is the last variable in slm_server's `.env` dead?** Almost certainly — its consumer was removed by
  FRE-1071, and the ES write path is confirmed stopped from here. Needs a grep in the slm_server repo,
  which is on the Mac and unreachable from the VPS.
- **Do not re-verify the OTLP chain.** FRE-1230 closed on a full Mac-side nonce procedure, corroborated
  here. Tempo needs an **explicit time window** or it answers a different question and looks empty.
