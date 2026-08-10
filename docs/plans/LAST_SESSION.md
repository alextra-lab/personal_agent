# Last session — 2026-08-09/10 (the OTLP chain closed, and five silent failures found by the owner)

## Doing / discussing

The Grafana migration program and the OTLP ingress chain, both to completion. The chain that had been
blocked since FRE-1071 merged against a precondition nothing provisioned is now live end to end and
deploy-verified. The open thread is the **Grafana rebuild program**: T4 (FRE-1209) is in flight on
build2 and is the exemplar eight T5 rebuilds copy, so a flaw there multiplies.

## What was decided and why

**The owner reversed a twice-confirmed decision on new information, and the distinction matters.**
`grafana_ro` was granted `SELECT` on all of `public`; the owner had confirmed that broad grant twice,
fully informed. Codex then recommended narrowing, and the reversal turned on **cost, not threat** — the
justification for breadth was "avoid a follow-up migration", and that is false, since FRE-1210 ships
migration 0026 regardless. The threat reasoning was never wrong: Grafana is loopback-bound behind
Access scoped to the owner. Returning with a changed cost is legitimate; re-litigating the threat would
not have been.

**A ticket's own comment thread can invalidate it, and nothing forces anyone to read that.** FRE-1011
was built, then cancelled at the gate: its thread carried three recorded rescope-or-close
recommendations since 2026-07-29, and FRE-1086 had already closed the corruption class by configuration.
Verified empirically — PR 884 named four tickets in its body and moved none. A Haiku cycle was spent
because master labelled it into a stream without reading the thread. **Read comments before labelling,
not only at the PR gate.**

**Decidability must be checked against the data, not just the wording.** FRE-1209's AC-4 requires a
divergent `cost_live_usd` vs `cost_authoritative_usd`; the live table has 77 reconciled rows and **zero**
divergent. Caught at dispatch and routed to the isolated test substrate rather than at the gate, which
is the FRE-1221 shape.

**Two "pre-existing failures on main" claims from builds were both false** — they were artefacts of a
worktree or `.env`-free checkout missing a gitignored file. Main was clean both times. Treat that claim
as needing verification, not as inherited fact.

**Rollout order was the risk twice, not the change.** FRE-1203's compose guard would have made every
cloud compose command unrenderable if merged before setting the env var; FRE-1238's untracking commit
deleted the live `budget.yaml` on pull, exactly as codex predicted, on master's own path. Both were
handled by acting before merging.

**Master's own operational failures, and they are the ones to carry forward.** Single-service
`make rebuild SERVICE=caddy` caused two incidents in twelve hours: it orphaned Filebeat's container-id
tail (FRE-1243) and let a dynamic container squat Caddy's declared static IP, making Caddy unstartable
(FRE-1244). **Never recreate Caddy outside a full-stack `up -d`.** And master declared a lost credential
"unrecoverable from the VPS" after searching the filesystem, `/tmp`, container envs and Docker volumes —
it was in `pass` the whole time, and the owner had to prompt twice to widen the search. The rotation was
unnecessary.

## Worktrees — anything special

All four seats hold merged branches the local delete could not remove; harmless. **build1's worktree was
found missing `budget.yaml`** after FRE-1238 untracked it — reseeded by hand. Any worktree created from
now on needs that file copied in, or `make test` shows ~14 failures that are not real.

## Sequence position + drift

On the console's Observability directive throughout; no console write this session, and it sits at 41 of
its 60-line bound. One deliberate deviation: master split FRE-1223 and filed FRE-1239 for the VPS-only
half, because FRE-1223's acceptance is stated as Mac-executed and would have parked a seat on owner
work. The owner authorised the split.

## Answers for the fresh start

- **Is the OTLP chain finished?** Yes, and proven with real traffic: a trace rooted at `seshat-vps`
  `POST /chat/stream` carries `slm-server` spans, so cross-process propagation works. `slm-requests-*`
  is at zero. Do not re-verify it; FRE-1071 and FRE-1224 carry the evidence.
- **Why is FRE-1223 still In Progress?** The Mac session owns it. Its Cloudflare half is demonstrably
  done — the tunnel rule exists and Access enforces — so it is a bookkeeping lag, not work.
- **What about FRE-1230?** The restart gate. The restart happened and succeeded; the Mac session owns
  closing it. FRE-1242 (removing slm_server's now-dead CF credentials) is its natural follow-on and is
  In Progress.
- **The unanswered question the owner never ruled on:** 21 Approved tickets in Observability Foundation,
  only a handful ever stream-labelled. Master offered a labelling pass to sequence the rest; no answer
  yet. Worth asking again rather than dispatching one at a time.
- **Why does FRE-1077 keep coming up?** Five silent-failure instances in two days, all the same shape —
  mechanism healthy, signal correct or absent, the *owner* noticed rather than the machinery. The worst
  was a 13-hour SLM outage where the health probe reported the exact error every five minutes and
  nobody read it. FRE-1077 is Urgent and still unlabelled.
