---
name: master
description: Use in the master session to integrate a ready PR — review, merge, deploy per the trust ladder, verify live, close the ticket, advance dispatch.
---

# Integrate a PR (master session)

Read `.claude/skills/lifecycle-rules.md` first. Argument: a PR number, or omitted (scan open PRs).

## 1 — Pick the PR
Lead with **`Gating PR #X →`** so the owner always sees which PR is at the gate. Read the PR
body, commits, and the linked ticket **including its comment thread** — comments carry the live
decision trail (owner steering, scope changes, runbooks) that the PR body often omits.

## 2 — Review
The code-review + security-review already ran in the working session (shift-left); its handoff
comment carries the **self-review summary** — diff class (self-serve / escalated), security
verdict, findings fixed or deferred. Validate it (spot-check, don't re-run the work), and add
your own light pass for what a diff-scoped review misses: scope creep, doc drift,
acceptance-criteria adherence. Bounce rules:
- Real-logic diff (src / script / behavioural config) with no self-review summary → bounce.
- `src`-logic / schema / security / cost diff with no codex plan-review noted → bounce (mis-tiered).
- Diff class `escalated` with no owner `/code-review ultra` on the thread → ask the owner
  before merging (their typed, billed invocation — never run it yourself).
- Fold-ins that support the ticket are expected; bounce one only if it's risky or unrelated.

## 3 — Gate
`python -m scripts.pr_gate <PR#>` collects the raw signals (CI check states, mergeability,
dependabot authorship); it renders no verdict — you decide. Gate on:
- CI green; PR checklist is pre-merge-only (post-deploy items → Linear comment after merge).
- **Each acceptance criterion written on this ticket has evidence it is delivered end to end** —
  an observed value (test result, probe/query output, observed behaviour), not an assertion
  that it was checked. A feature ticket with no criteria → bounce. A bugfix needs no ADR
  provenance but still needs its reproducing test or verification.
- If a backing ADR exists: the diff implements it **as designed**; silent divergence bounces.
  (If the design genuinely changed, the ADR document is updated first.)

**Bounce channel:** a direct `send-keys` message to the worker's `cc-<stream>` seat naming the
PR and the fix; written detail in a PR comment. The seat is warm and self-completes.

## 4 — Merge
`gh pr merge <n> --merge --delete-branch` with a review summary; `git pull` on main.

## 5 — Deploy authorization
Class membership: **reversible** (PWA-only rebuild — bump `CACHE_NAME` first · additive ES
template · Grafana dashboard import) vs **everything else** (`seshat-gateway` rebuild · ES
type-change/reindex · Postgres schema/migration · cost/budget/governance). Look the class up in
the trust ladder (`docs/plans/OWNER_CONSOLE.md`) and act at its recorded level. Unsure → the
stricter class. A deploy grant is never a budget grant. Confirm timing if another session is
mid-flight on the same service.

## 6 — Deploy
`ENV=cloud make rebuild SERVICE=<svc>` (VPS; `make deploy` is Mac-only).

## 7 — Verify
`curl -s http://localhost:9001/health` + the affected endpoint; paste status + body. If the PR
touched an emit site / schema / cost / memory write: run
`scripts/monitors/joinability_probe.py` and paste output. Never claim done from
"deploy exited 0" alone.

## 8 — Close out (same session as deploy, never deferred)
- Ticket → `Done` + the short close comment (lifecycle-rules § Tickets): PR link · merge SHA ·
  deploy class + who authorized · what was verified and the observed result.
- Verification failed → `Verify Failed` (never Done, never left in Awaiting Deploy), file the
  follow-up, consider rollback.
- **Advance dispatch at every MERGE** (the merge is what frees the stream): move the merged
  ticket → `Awaiting Deploy`, then run
  `python -m scripts.dispatch.next_resolver --stream <s> --eligible --json` for the affected
  stream(s) — nonzero exit, invalid JSON or a printed error → STOP and surface stderr; never
  reconstruct the ordering inline. Then, per the resolver's output:
  - Remove `blockedBy` relations satisfied by this merge; audit a newly-labeled ticket for a
    stale relation to an already-terminal blocker.
  - Label the next head (`stream:<s>`) after the one pre-label check: its criteria are
    verifiable from its own deliverable.
  - Exactly one intended head per stream, pinned by priority (High = head, Urgent = front-jump);
    never left to the creation-date tie-break. A front-jump needs no relation edits; a mid-chain
    insert is a relation edit.
  - Set `context:keep` per the build's context-disposition note.

## Identity
Delivery guardian: proof before Done, the board never lies, and briefings are decision-support —
verify before you assert, frame the ask as a decision, give exact commands, bring a
recommendation. Fix small things yourself instead of filing tickets. Never use the injected CC
`userEmail`; use the owner's designated test email for gateway calls.
