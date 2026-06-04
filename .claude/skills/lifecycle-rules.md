# Lifecycle Rules (shared by /build, /master, /adr, /prime-master)

These invariants are the single source of truth. Role skills reference this file;
they MUST NOT restate or fork these rules. Coding standards live in `.claude/CLAUDE.md`.

## PR hygiene
- A PR checklist contains **pre-merge items only**.
- FORBIDDEN in a PR checklist: post-deploy verification, telemetry checks, deploy
  steps, "verify on prod after merge". Those belong in a Linear comment after merge.

## Session boundary
- build & adr sessions stop at "push branch + open PR". They never merge, deploy,
  close tickets, or edit MASTER_PLAN.
- master alone merges to main, deploys, runs live verification, closes Linear
  tickets, and updates MASTER_PLAN.

## MASTER_PLAN
- Committed to `main` only — never a feature branch.
- "Last updated" line is bumped every time a ticket ships.

## Ticket state
- Implement only `Approved` tickets (verify via Linear `get_issue`).
- Deferred or parked work is marked deferred, NEVER Done.
- New issues are created in state "Needs Approval", under a Linear project.

## Deploy
- Deploy is a master-only, owner-approved action. Approving a PR or a fix does NOT
  authorize a deploy. Confirm deploy timing explicitly, especially with a concurrent
  session active.

## Halt conditions (stop and surface; do not work around)
- Ticket not `Approved`.
- Pre-existing worktree on an unexpected branch.
- Plan would bundle multiple ADR phases into one PR (one phase = one PR).
- Plan would drop/quarantine historical rows — surface row count, get explicit confirmation.
- `make mypy` shows >5 errors you did not introduce (likely a main-green issue; separate ticket).
- Deploy succeeds but the live endpoint returns the wrong response — file a follow-up; do not mark done.
- Joinability probe finds orphans — do not mark done; file a follow-up.
- Same error recurs after 3 fix attempts — escalate per MODEL_ROUTING_POLICY.
