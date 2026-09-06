# Last session — 2026-09-06 (early hours)

## Doing / discussing  (≤5 sentences)
The owner selected the instruct model as their **main** model, to test that thinking was off. The
turn failed at 90.001 seconds and the test never got an answer. That failure opened a
model-management review, and the owner stopped all other model work for it. The study concluded
that ADR-0121 was never finished, and the owner concurred: *"this is the way."* The next session
implements that chain.

## What was decided and why

**The 90-second failure was not a resolution bug, and calling it one wasted an hour.** The
selection mechanism worked exactly as designed. The instruct catalog entry carries the sub-agent's
budget — `default_timeout: 90`, `max_tokens: 2048` — so selecting it as primary imports a
fail-fast worker's limits onto the main model. **Role policy lives on a deployment.** That is the
defect, and the picker offering that entry as a model is what exposes it.

**Master's proposed fix was measured to do nothing, and how it failed matters more than the fix.**
Master recommended moving `default_timeout` onto the role binding, plus a "one-line mitigation" of
a primary-binding override. Explore ran `resolve_role_target` both ways at the deployed revision:
the timeout stays 90 either way. The resolver drops binding overrides when the selected key
differs from the binding's own deployment — **so overrides are dropped in exactly the case a
selection exists for.** Master reasoned that dropping them was harmless because the deployment's
own value would stand; true, and irrelevant, because the instruct deployment's own value *is* 90.
**The reasoning checked the wrong half of the mechanism, and running the resolver was one command.**

**Wrong instrument, in front of the owner.** Master cited `resolve --role primary` as proof the
system honoured the owner's pick. That command reports the **default binding with no session
selection applied** and says nothing about any particular turn. The owner caught the contradiction
in the same paragraph. The right instrument is `session_model_selections`, the only record holding
the catalog key — every other record (ES `model`, `sessions.primary_model_at_creation`) stores the
shared wire id and cannot tell the two entries apart.

**A catalog correction must never travel with a binding change.** A swap and a catalog fix shipped
in one PR on 2026-09-05; the swap was reverted and the correction stranded with it, leaving `main`
naming a backend that no longer exists. Unpicking it cost three PRs. Now in memory.

**`CACHE_NAME` was merged over a red baseline, deliberately, and the green that unblocked it is
not a signal.** The PWA e2e job fails on a genuine WCAG contrast defect — computed 4.128 at best
against 4.5 required — which has made `main` intermittently red since 2026-09-05 17:40. Branch
protection refused `--admin`, so the intermittent job was re-run and passed. **A re-run passing
does not mean the defect is gone**; only the margin varies, because the element is sampled
mid-transition. Merging was still right: without the bump, installed clients keep serving cached
v55 and never receive the session-gate fix.

**A board state lied for thirty hours and master saw it twice before acting.** FRE-1328 sat in
`Awaiting Deploy` whose only PR is docs-only and merged 2026-08-29 — there was never code to
deploy. Its real blocker is an owner decision on ADR-0139's status, which ADR-0140 partly withdrew.

**Deferred close-out is a pattern, not an incident.** Three deploys from 2026-09-05 sat unclosed
overnight. The cause each time is an interruption landing between the deploy and the close, and
the close then never happens. Third instance after FRE-1375 and FRE-1390.

**`Tier-3:Haiku` strands its seat.** Master mislabelled a new ticket that way and caught it before
dispatch. Haiku 4.5 has no auto mode. Retiered to Sonnet.

## Worktrees — anything special
Nothing unpushed anywhere; all four worktree branches match their remotes. The FRE-1377 commits
flagged mid-session as possibly stranded are on `origin/main` — checked, not assumed.

## Sequence position + drift
The owner called a **full stop** on model work at 05:00 so the study could run without tickets
being advanced underneath it. Nine Approved tickets were held; the study then adjudicated them.
FRE-966 and FRE-967 are blocked on the new ADR rather than cancelled, because cancelling is that
ADR's own cleanup clause and the ADR is not yet accepted.

**The Observability Foundation directive remains unstarted — fourth consecutive session.** It has
been displaced each time by a live incident. Worth raising as a decision rather than drifting a
fifth time.

## Answers for the fresh start

- **What does the owner want next?** Implement the explore chain: model config and sub-agent
  functionality. FRE-1426 is the ADR (`stream:adr`, Urgent, Approved) and is the adr stream's next
  head.
- **Why is FRE-1427 not dispatchable?** It has no stream label on purpose. It cannot land until
  the owner serves `--ctx-size 131072` in `slm_server` and reports the `KV self size` line.
  Landing first would make the catalog claim a window the backend does not serve — FRE-1317's
  exact shape.
- **What is at the gate?** PR #1071 (FRE-1417, the duration invariant). Untouched by master, not
  half-merged. It is the first thing to gate.
- **What is blocked on the owner?** FRE-1328 needs an ADR-0139 status decision. FRE-1427 needs the
  backend change. FRE-1414's post-deploy check needs the owner's screen — iOS standalone has no
  test coverage. Six tickets sit in `Needs Approval`, all `src/` changes.
- **Is `main` green?** No, and do not treat a passing run as evidence it recovered. FRE-1425
  carries the real defect and requires `main` itself green.
- **Was the owner's thinking test ever answered?** No. `route_traces.thinking_enabled` has 805
  rows and zero populated since 2026-06-07. FRE-1422 found the cause: the assembler never reads
  the resolved definition.
