# Last session — 2026-09-06 (day)

## Doing / discussing  (≤5 sentences)
The owner said **"I want the model work completed"** and the day delivered it: two studies, an
ADR, and an eleven-ticket chain now executing two-wide. The owner also asked why nothing was
moving, and the honest answer was master. Four of master's own assertions were wrong today and
each was corrected in the record rather than merged past. The next session gates the chain's PRs
as they arrive.

## What was decided and why

**Master was the bottleneck, not the machinery — and this is the session's main lesson.** At
11:38 the owner asked why nothing was moving. Every worker seat was idle *because it had
finished*: two green PRs had sat at the gate, one for six hours, while master ran a provider
investigation. Master leaned on the watcher instead of scanning open PRs, which is exactly the
thing it is told not to do. **Scan `gh pr list` at every natural pause.**

**Four master assertions were wrong today, and they are one failure, not four: asserting a
mechanism without running it.** (1) Master told the owner `temperature: 1.0` sets what the OVH
model runs at — the cloud branch never reads the catalog's temperature (`litellm_client.py:929`
against the local fallback at `:1498`). (2) FRE-1425 was filed asserting "4.128 is a real AA
failure at rest"; the settled pairing measures **4.624** and both samples were mid-animation, so
master's own ticket instruction would have bounced the correct fix. (3) Master told the owner
"your comment said X" — a previous *master* session wrote it, and its own prose said so twice.
(4) Master blamed litellm's missing cost-map record for a three-day-old model; the omission is
**provider-level**, so the model's age was never the cause. This is the same shape FRE-1421 had
just finished documenting.

**A deferral instruction from master created a real defect, and the adr seat caught it.**
Master told the seat to defer D3 whole. `RoleBinding` carries `temperature` but no `top_p` and
no `presence_penalty`, so deleting the `-instruct` entry with D3 deferred strands its
`0.7 / 0.8 / 1.5` preset and the worker silently inherits the thinking preset. Instruction
withdrawn. **A seat that pushes back with code references is usually right.**

**Placement was doing two jobs, and separating them is what ADR-0145 is.** `placement` decided
both *where* a model runs and *which parameter dialect* we speak to it. OVH is the counterexample
that separates them — cloud-placed, open-weights, accepts OpenAI-standard fields and refuses
every Qwen-native one. Measured, not inferred: `chat_template_kwargs` is refused by OVH;
`reasoning_effort` works but litellm blocks it; `allowed_openai_params` defeats that.

**We create tickets roughly three times faster than we close them, and the engine is ADR
decomposition.** 20 created against 7 Done on 2026-09-06. One ADR yields seven to eleven tickets;
three streams execute one at a time. The owner raised this directly. The `OWNER_CONSOLE` backlog-
cull directive has sat unretired for five weeks and master offered to scope it — **not approved,
not drafted.**

**Two tickets are held open deliberately and must not be "fixed".** FRE-1402 and FRE-1427 are
merged, deployed and health-verified; their remaining criteria need live turns with a human
reading the answers, which master does not fire unasked. FRE-1398 and FRE-1426 are docs-only ADRs
whose criteria the implementation chains deliver. All four state this on the ticket.

## Worktrees — anything special
Nothing unpushed. All four branches match their remotes. `telemetry/dispatch_state.json.bak-163808`
is untracked and pre-existing — the owner's, left alone.

## Sequence position + drift
The model/agent/subagent fast-track directive of 2026-09-05 is being executed, not drifted. The
**Observability Foundation** directive was raised as a decision at 08:15 and the owner deferred
it — "we will get back to it" — so it is now a recorded deferral rather than a fifth silent slip.

**One unresolved item the owner authorised but master could not complete.** The explore seat froze
twice on read-only `docker exec` permission prompts. The owner said "It is permitted", but the
auto-mode classifier blocked master from editing `settings.local.json`, and master declined to
route around a guard on a permissions file. **The five scoped allow rules are still not in place**
(`/opt/seshat/.claude/worktrees/explore/.claude/settings.local.json`); the owner must add them via
`/permissions` in that pane.

## Answers for the fresh start

- **What is running?** build2 FRE-1439 (the chain root — nine tickets block on it), build1
  FRE-1405, adr FRE-1361. The lane rationale is a comment on FRE-1439 — **read it before
  re-shuffling any `stream:` label.**
- **Is `main` green?** Yes. The PWA e2e job that was red since 2026-09-05 17:40 passes; the cause
  was a theme-init repaint race, not a contrast defect.
- **What is blocked on the owner?** FRE-1402's three live probes · FRE-1427's AC-2 (the
  `KV self size` line at 262144, which may no longer exist) and AC-3 (a ~105K turn against the new
  98304 budget, the case that can genuinely fail) · the explore permission rules · the backlog-cull
  scope.
- **Why is a `served_catalog_drift` warning in the gateway logs?** It is correct and deliberate.
  FRE-1447 shipped a detector against a real drift left unfixed on purpose. **It should disappear
  when FRE-1445 lands** — treat it as a live regression test for the collapse, not a defect.
- **Do the catalog and the backend agree?** Yes, at 131072, verified live after FRE-1427.
