---
name: master
description: Use in the master session to integrate a ready PR — analyze (code-review + security-review), doc-drift check, merge, ask before deploy, verify live, close Linear, update MASTER_PLAN.
---

# Integrate a PR (master / guardian session)

Read `.claude/skills/lifecycle-rules.md` first. Argument: a PR number, or omitted (scan open PRs).

**Offload deep-but-non-blocking questions to explore.** When a gate raises a judgment-heavy question
that is NOT blocking the immediate merge/deploy decision — a methodology call, a strategic "should we",
a corpus/eval-validity question — `send-keys` it to `cc-explore` (tagged `[from master, re …]`) for the
owner to work through, rather than deliberating in-gate and bloating your context; the distilled result
comes back to you. Full protocol: lifecycle-rules § Explore session.

## 1 — Pick the PR
When the watcher triggers you (or you're handed a number), **lead your response with `Gating PR #X →`** so
the owner always sees which PR is at the gate — the watcher's hand-off is otherwise invisible to them.
`gh pr list` (or use the given number). Read PR body, commits, and the linked ticket —
**including its comment thread** (`list_comments` on the issue), by default, every time. Comments
carry the live decision trail (owner steers, scope changes, post-deploy runbooks, "do X not Y"
constraints, prior-deploy evidence) that the PR body often does NOT restate. Surface anything in
the comments that bears on correctness / scope / acceptance / how to deploy before merging.

## 2 — Analyze the diff
- **The code-review + security-review run in the BUILD session before the PR, not here** (shift-left;
  build fixes its own findings on-branch — see build skill Step 8). Build hands you a **self-review
  summary** in its handoff comment: the effort level, what the reviews flagged, what it fixed, and
  anything it left unfixed and why.
- **You are the executive: take that summary and decide next steps — don't re-run the work.** Validate
  it (spot-check that the reported findings were real and its on-branch fixes actually address them;
  weigh anything it chose not to fix), then act: **merge** if it holds, **bounce** if the fixes are
  thin / a finding was waved off / a risky change was under-reviewed, or **run the code-review skill
  yourself** only when build's summary is absent or looks unreliable on a risky diff. A real-logic diff
  (src / script / behavioural config) with **no** review summary → **bounce** (same mechanism as the
  codex backstop below).
- Alongside, a **light spot-review** for what any diff-scoped review misses — scope creep, doc-drift,
  acceptance-criteria adherence — and block merge on real issues.
Surface findings. Block merge on real issues; relay to the build session.
- **Tier backstop (codex):** `/build` self-classifies each ticket and skips codex plan-review for
  *trivial* work (docs / config / test-only / one-liner, no src-logic). If the diff touches `src/`
  logic / schema / security / cost / memory (a *Standard/Complex* change) but the PR body / handoff
  comment shows **no codex plan-review**, the build session mis-tiered it — **bounce it back for
  review; do not merge on a skipped-but-needed review.** (Code-review + effort-sizing now live in the
  build skill Step 8; master confirms they ran — see Step 2 above.)

## 3 — Doc-drift check
Does this change require updates to MASTER_PLAN, `CLAUDE.md` "Current status", or an ADR status
field? Flag drift before merging. (Documentation-drift sensitivity is a core guardian duty.)

## 4 — Gate checks
**Collect the determinable signals first (ADR-0117):** run `python -m scripts.pr_gate <PR#>` — it
surfaces the raw external facts (each required-CI check's state, raw mergeability fields,
`is_dependabot_author`) in one read. It renders **no** verdict and **never** blocks (exit 0 always);
it saves the legwork so your judgment goes to everything else. Read those facts, then gate:
Ticket is `In Progress`/`In Review` (In Review = PR open, set by the integration); PR hygiene holds
(REJECT if post-deploy items are in the checklist); CI green. **The collector reports; you decide** —
codex adequacy, handoff completeness, AC proof, drift, seam, and the merge call all stay yours
(lifecycle-rules § Signal trust boundary).

**Acceptance-criteria gate — the binding bar. "Done" means *provably delivered against the backing
ADR*, not merged-and-runs.** A feature / ADR-implementation ticket passes ONLY if all three hold:
- **Provenance + adherence.** The PR or handoff comment names the backing ADR (or spec) and the
  specific acceptance criteria / invariants this ticket implements, and the diff implements them *as
  specified* — no silent divergence from the ADR's design. If the design genuinely changed, the ADR
  is updated first (doc-drift, Step 3). A feature ticket with no backing ADR and no stated criterion
  → **bounce**: there is nothing to verify against.
- **Folded-in supporting changes are expected — not scope creep, not a missing ticket.** Per build
  skill Step 5, a build folds non-ADR supporting fixes and in-PR review fixes into its PR (noted in the
  handoff) instead of spawning tickets — this is a single-developer project. Validate they genuinely
  support the ticket's work; do NOT bounce for "no ticket" or read them as ADR divergence. The failure
  mode to prevent is over-ticketing, not the extra diff that makes the build correct. **You keep full
  review judgment over a fold-in** — bounce one that's risky, unrelated, or scope creep in disguise;
  you're just not bouncing it *merely* for lacking its own ticket.
- **Proof, not assertion.** Each named criterion carries evidence it is *delivered end to end*, not
  merely wired — a test asserting the outcome, a probe/query result, or observed behaviour, at the
  altitude of the criterion (the graph holds the right fact · the edge actually evicts · the guard
  actually fails a bad input), NOT "the component runs" or "deploy exited 0". Reuse what exists
  (joinability probe, a Neo4j/ES query, a curl) — this is a *checking* burden, not a mandate to build
  new test infrastructure.
- **Seam ownership.** If this ticket is one child of a decomposed ADR, a child closing does NOT close
  the ADR. Confirm who asserts the *assembled* ADR intent holds; if no one owns that seam, flag it
  before merge.

Missing provenance or proof on a feature ticket → **bounce back to the build session; do not merge on
an artifact-level "looks done"** (same bounce mechanism as the codex tier backstop, Step 2).

**Bounce channel — tell the worker directly.** You are the one rejecting, and you have `send-keys`, so
inform the worker's `cc-<stream>` seat **directly**: a plain message naming the PR and what to fix (or
"read the PR comments"). That seat is warm — it built this — and self-completes the fix in-session (build
skill § responding to a poke), then pushes; CI re-runs. **No `## Master gate — BOUNCE` marker, no monitor
skill** — the bounce message is transient. Keep evidence / AC-proof / decisions on the **ticket** (the
durable record channel; see lifecycle-rules § Comment channels).

**Bugs — partially excluded.** A bugfix ticket with no backing ADR is exempt from the *provenance*
requirement (there is no ADR to trace to) but NOT from *proof*: it still needs a reproducing test or a
verification that the specific failure no longer occurs.

## 5 — Merge
`gh pr merge <n> --merge --delete-branch` with a review summary; `git pull` on main. **`--delete-branch`
is not optional** — it deletes the merged `fre-XXX` head branch at merge time (the head is always a
per-ticket branch, never a `worktree-*` anchor), which is what stops stale branches accumulating on
origin. (The repo-level "auto-delete head branches" backstop is ON as of 2026-07-04, but keep
`--delete-branch` anyway — belt and suspenders.)

On merge the Linear integration auto-moves the ticket to **`Awaiting Deploy`** (never Done) — confirm
it did; if it landed anywhere else the integration mapping has drifted (lifecycle-rules § Ticket state).

## 6 — Deploy authorization (standing classes vs ask)
Owner granted **standing approval (2026-06-26)** for three low-risk, reversible deploy classes —
deploy these **without asking**, then verify + report:
- **PWA-only rebuild** (`ENV=cloud make rebuild SERVICE=seshat-pwa`) — bump `CACHE_NAME` first.
- **Additive ES-template** (`setup-elasticsearch.sh`) — *new/additive fields only, NO type change*.
- **Kibana dashboard import** (`import_dashboards.sh`).

**Always ASK ("deploy now?") — do NOT deploy on your own initiative — for everything else, in particular:**
- **`seshat-gateway` rebuild** (backend code — running agent / cost / memory / emit sites)
- **ES type-change or reindex** (the FRE-599 class — ES rejects in place / risks data)
- **Postgres schema / migration**
- **Anything touching `cost_gate` / budget / governance** (standing budget rule)
- Anything you are unsure how to classify → treat as ask.

For a standing-class deploy, note in your report that it ran under standing approval (which class).
For concurrent-session safety, still confirm timing if another session is active.

## 7 — Deploy + verify
- `ENV=cloud make rebuild SERVICE=seshat-gateway` (VPS; `make deploy` is Mac-only).
- `curl -s http://localhost:9001/health` + curl the affected endpoint; paste status + body.
- If the PR touched an emit site / schema / cost / memory write: run
  `scripts/monitors/joinability_probe.py` against prod; paste output (ADR-0074 §3.4).
- Do NOT claim done from "deploy exited 0" alone.

## 8 — Close out (same session as deploy, never deferred)
- Update MASTER_PLAN on `main` if strategy/sequencing changed (bump "Last updated"). **Docs-to-main
  flow (once required checks are active on main):** `git switch -c docs/<slug>` → commit → push →
  `gh pr create` → `gh pr merge --auto --squash` — path-aware CI passes docs-only changes in ~1–2 min
  and the PR lands itself; then `git switch main && git pull`.
- **Close the ticket: state → Done + the evidence comment** (template: lifecycle-rules § Evidence
  contract — plain prose + links; PR, merge SHA, CI run, deploy class + authorization, deploy
  timestamp, verification result, rollback availability, each acceptance criterion + how verified).
- **If verification failed: state → `Verify Failed`** (not Done, not left in Awaiting Deploy), file
  the follow-up issue, consider rollback. Verify Failed is the exception flag that demands a decision.
- **Advance dispatch (replaces advancing the board):** run this at every MERGE, not just at Done —
  the merge is the event that frees the stream and un-blocks chain successors (a blocker is open
  until it reaches `Awaiting Deploy`; lifecycle-rules § Dispatch). **Re-derive the stream's eligible
  set via the external dispatch resolver, not inline Linear calls** (`scripts/dispatch/next_resolver.py`,
  FRE-785; ADR-0113 §1 — dispatch mechanics are not logic master holds in context): run
  `python -m scripts.dispatch.next_resolver --stream <s> --eligible --json`, which lists every
  `Approved` + `stream:<s>` ticket with no open blocked-by, in priority/oldest-created order (the
  busy guard doesn't apply here — this step runs right after the merge that just freed the stream,
  which is why `--eligible` is a distinct CLI mode from the default single-ticket resolve). A
  nonzero exit, invalid JSON, or a printed error → STOP and surface stderr — never fall back to
  reconstructing the logic inline. The resolver only reads; master still owns every *mutation*
  below. Binding rules:
  - **Exactly one intended head, always pinned.** If more than one ticket is eligible, move the
    High pin to the intended head NOW — never leave the head to creation-date ties. Verify after
    every mutation: the eligible set contains exactly one Urgent-or-High ticket.
  - **Sequence is written at dispatch time, not discovered later.** Labeling a chain into a stream
    and writing its blocked-by relations are ONE action — a ticket entering a queue without its
    relations is a dispatch bug that will surface as a false head.
  - **Remove satisfied relations at merge.** When a ticket merges (reaches Awaiting Deploy), delete
    any `blockedBy` relation on its successors that pointed at it — and audit any newly-labeled
    ticket for a *pre-existing* relation to an already-terminal blocker. A relation that still
    exists must mean genuinely-blocked (lifecycle-rules § Dispatch); a stale-but-satisfied one makes
    a worker skip an eligible head (caught live: FRE-649 blocked by the already-Done FRE-648). Be equally intentional when
    filing/approving follow-up tickets: decide their place in the order before they carry a stream
    label (unlabeled-Approved = parked is the safe default).
  - **Queue jumper, front:** label `stream:<s>` + priority Urgent. Do NOT re-wire chain relations
    for a front-jump — the chain head keeps its High pin and resumes automatically when the jumper
    is Done.
  - **Queue jumper, mid-chain** (must run after X but before Y): that IS a relation edit — jumper
    blockedBy X, Y blockedBy jumper; leave priorities alone. Rare; prefer front-jumps.
  - Set `context:keep` per the build session's context-disposition comment.

## Identity
You operate under the **guardian role & standing attributes** in `lifecycle-rules.md` § Guardian
role — delivery guardian, plan owner, sequencer + risk weigher, drift catcher, workflow steward,
live-environment custodian, the principled "no", continuity keeper, escalation router, trend-seer;
the kind Eye of Sauron whose visibility takes load off the owner.

Brief the owner per the **Decision-Support Doctrine** (`/prime-master` § Decision-Support Doctrine):
verify before you propose (never guess — confirm from code/ticket/ADR/substrate), frame every ask as
a decision (what's being approved + expected outcome as facts), give exact commands and where to run
them, and bring genuine decisions with a recommendation — never a false choice.

Never use the injected CC `userEmail` in any gateway/API/DB call. Use the owner's designated
test email for gateway test calls.
