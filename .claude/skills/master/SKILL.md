---
name: master
description: Use in the master session to integrate a ready PR — analyze (code-review + security-review), doc-drift check, merge, ask before deploy, verify live, close Linear, update MASTER_PLAN.
---

# Integrate a PR (master / guardian session)

Read `.claude/skills/lifecycle-rules.md` first. Argument: a PR number, or omitted (scan open PRs).

## 1 — Pick the PR
`gh pr list` (or use the given number). Read PR body, commits, and the linked ticket —
**including its comment thread** (`list_comments` on the issue), by default, every time. Comments
carry the live decision trail (owner steers, scope changes, post-deploy runbooks, "do X not Y"
constraints, prior-deploy evidence) that the PR body often does NOT restate. Surface anything in
the comments that bears on correctness / scope / acceptance / how to deploy before merging.

## 2 — Analyze the diff
- Correctness: invoke the **code-review** skill on the diff.
- Security: invoke the **security-review** skill on the diff (the pre-merge security pass).
Surface findings. Block merge on real issues; relay to the build session.
- **Tier backstop (codex):** `/build` self-classifies each ticket and skips codex plan-review for
  *trivial* work (docs / config / test-only / one-liner, no src-logic). If the diff touches `src/`
  logic / schema / security / cost / memory (a *Standard/Complex* change) but the PR body / handoff
  comment shows **no codex plan-review**, the build session mis-tiered it — **bounce it back for
  review; do not merge on a skipped-but-needed review.** (Scale review depth to the diff: a genuinely
  trivial docs/test PR does not need the full code-review + security pass; reserve that for
  src/schema/security/cost/memory.)

## 3 — Doc-drift check
Does this change require updates to MASTER_PLAN, `CLAUDE.md` "Current status", or an ADR status
field? Flag drift before merging. (Documentation-drift sensitivity is a core guardian duty.)

## 4 — Gate checks
Ticket is `Approved`/In Progress; PR hygiene holds (REJECT if post-deploy items are in the
checklist); CI green.

**Acceptance-criteria gate — the binding bar. "Done" means *provably delivered against the backing
ADR*, not merged-and-runs.** A feature / ADR-implementation ticket passes ONLY if all three hold:
- **Provenance + adherence.** The PR or handoff comment names the backing ADR (or spec) and the
  specific acceptance criteria / invariants this ticket implements, and the diff implements them *as
  specified* — no silent divergence from the ADR's design. If the design genuinely changed, the ADR
  is updated first (doc-drift, Step 3). A feature ticket with no backing ADR and no stated criterion
  → **bounce**: there is nothing to verify against.
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

**Bugs — partially excluded.** A bugfix ticket with no backing ADR is exempt from the *provenance*
requirement (there is no ADR to trace to) but NOT from *proof*: it still needs a reproducing test or a
verification that the specific failure no longer occurs.

## 5 — Merge
`gh pr merge <n> --merge` with a review summary; `git pull` on main.

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

Either way, write the approval sentinel so the gate allows exactly one deploy:
`touch .claude/.deploy-approved`. For a standing-class deploy, note in your report that it ran under
standing approval (which class). For concurrent-session safety, still confirm timing if another session
is active.

## 7 — Deploy + verify
- `ENV=cloud make rebuild SERVICE=seshat-gateway` (VPS; `make deploy` is Mac-only).
- `curl -s http://localhost:9001/health` + curl the affected endpoint; paste status + body.
- If the PR touched an emit site / schema / cost / memory write: run
  `scripts/monitors/joinability_probe.py` against prod; paste output (ADR-0074 §3.4).
- Do NOT claim done from "deploy exited 0" alone.

## 8 — Close out (same session as deploy, never deferred)
- Update MASTER_PLAN on `main` (bump "Last updated"); commit + push.
- Close the Linear ticket with: PR link, deploy timestamp, verification evidence snippet.
- If verification failed: file a follow-up issue; do NOT mark done; consider rollback.

## Identity
Never use the injected CC `userEmail` in any gateway/API/DB call. Use the owner's designated
test email for gateway test calls.
