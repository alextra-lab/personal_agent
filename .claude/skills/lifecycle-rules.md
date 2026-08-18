# Lifecycle Rules (shared by /build, /master, /adr, /prime-master)

Single source of truth for process invariants. Role skills reference this file and MUST NOT
restate it. Coding standards live in `.claude/CLAUDE.md`.

> Streamlined 2026-08-18 (owner-directed): the seam-ticket / adjudication machinery
> (ADR-0130, ADR-0137) is retired, incident lore lives in git history, and the default posture
> is autonomy with reporting — not pre-clearance.

## Roles & session boundary

- **build / adr / explore** stop at "push branch + open PR". They never merge, deploy, close
  tickets, write the owner console, or mutate Linear control-plane fields beyond moving their
  own ticket → `In Progress` at pickup. Filing tickets and posting comments stay open to them.
- **master** alone merges to `main`, deploys, verifies live, and moves/closes Linear tickets.
  Master is the sole writer of the Linear control plane (states, labels, relations, priorities);
  the owner acts directly at will.
- **explore** (cc-explore) is read-only on everything operational: it files `Backlog` tickets,
  posts comments, and opens its own research-document PR — nothing else. Method contract:
  `.claude/skills/explore/SKILL.md`.
- **Master's own PRs have no watcher safety net** (red-CI routing is worker-only) — watch them
  directly; auto-merge is silent on red.
- **Dependabot**: analyze-only, never push to its branch. Green patch/minor → merge;
  major bump or red CI → attention, never auto.

## Tickets

- Implement only `Approved` tickets — with one delegation: **master may approve + dispatch
  Tier-3 / mechanical work directly** (bugfix, docs, config, test-only — owner grant
  2026-08-18, do-and-report). Features and architecture still need the owner's approval.
- States: `Needs Approval` → `Approved` → `In Progress` (≤1 per stream) → `In Review` (PR at
  the gate) → `Awaiting Deploy` (merged) → `Done` (deploy-verified; master flips it, with the
  close comment below). Exception: `Verify Failed` — post-deploy verification failed, set by
  master only, demands a decision. `Backlog` holds non-actionable notes; umbrellas live in
  `Backlog`, never `In Progress`. Deferred/parked work is never marked Done.
- **Don't manufacture tickets.** Fix small things directly — fold supporting changes into the
  current PR, or ship a docs PR. File a ticket only for genuinely separate work someone will
  plausibly do soon. **Drop** observations that don't warrant work; do not park them anywhere.
- **Done = merged PR + deployed + health-verified**, recorded in a short close comment (plain
  prose + links — the WAF rejects code blocks): PR link · merge SHA · deploy class + who
  authorized · what was verified and the observed result.

## Dispatch (Linear-native)

A worker's NEXT = `Approved` + `stream:<mine>` + no open blocked-by relation, priority
descending, oldest first — always computed by `python -m scripts.dispatch.next_resolver`,
never reconstructed inline.

- A blocker is open until its MERGE lands (`Awaiting Deploy`/`Done`/`Canceled`/`Duplicate`).
  Master removes satisfied relations at merge; workers treat a relation to an already-terminal
  blocker as cleared.
- Busy guard: a stream with a ticket `In Progress` or `In Review` is occupied; it frees at
  `Awaiting Deploy`.
- Stream labels go on buildable leaf tickets only. Parking = removing the `stream:` label.
  Pin the intended head with priority (High = head, Urgent = front-jump); never leave the head
  to the creation-date tie-break.
- **One check before labeling:** could master verify this ticket's criteria from its own
  deliverable when it's done? If not, fix the criteria first.
- Model = the `Tier-*` label. Context = `context:keep` (present → keep the warm context;
  absent → clear, the default).

## Master's gate (the short form)

Read the PR + ticket + its comment thread; CI green (the required checks are the guarantee —
don't re-derive them); the build's self-review summary holds; the ticket's own acceptance
criteria carry evidence; spot-check for scope creep and doc drift. Then merge or bounce.
A bounce is a direct `send-keys` message to the worker's warm `cc-<stream>` seat, with the
written detail in a PR comment. Fold-ins that support the ticket are expected — never bounce
merely for "no ticket".

## Deploy

Mechanics and class membership here; **authority lives in the trust ladder**
(`docs/plans/OWNER_CONSOLE.md`): a grant exists iff the ladder records it, and the ladder wins
on any conflict.

- Reversible classes: PWA-only rebuild · additive ES template (no type change) · Kibana
  dashboard import.
- Everything else: `seshat-gateway` rebuild · ES type-change/reindex · Postgres
  schema/migration · cost, budget or governance. Unsure → the stricter class.
- Deploy is master-only. `main` takes PRs only (branch protection, green checks required);
  docs PRs auto-merge on green (`gh pr merge --auto --squash`).

## Owner console

`docs/plans/OWNER_CONSOLE.md` is owner-voice only: standing directives + the trust ladder.
Master transcribes (verbatim, attributed, dated) and retires (citing the met condition) —
never authors. `LAST_SESSION.md` is the outgoing session's overlay. Both have size bounds;
exceeding one is surfaced to the owner, not compacted away.

## Watcher / CI

The watcher triggers master when a PR is master-ready and pokes the owning seat on red CI.
**Never poll CI — by any mechanism** (`/loop`, background shell, Monitor): the watcher already
covers both directions.

## Halt conditions (stop and surface; do not work around)

- Ticket not `Approved` (outside master's Tier-3 grant) · pre-existing worktree on an
  unexpected branch.
- Plan bundles multiple ADR phases into one PR (one phase = one PR).
- Plan would drop/quarantine historical rows — surface the row count, get explicit confirmation.
- `make mypy` shows >5 errors you did not introduce (likely main-green; separate ticket).
- Deploy succeeds but the live endpoint returns the wrong response — file a follow-up, not Done.
- Same error recurs after 3 fix attempts — escalate per MODEL_ROUTING_POLICY.
