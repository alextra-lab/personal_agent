# Seshat delivery pipeline — roles, controls, and topology

> Audience: the owner + DevSecOps / CI-CD reviewers. Describes how change flows from
> idea → approved ticket → PR → reviewed merge → gated deploy → verified live, and the
> separation-of-duties model that governs it.
> Maintained by the `master` (guardian) session. Last updated: 2026-07-04.

**Framing:** This is a **separation-of-duties model implemented across LLM agent sessions**.
Instead of one agent that writes *and* ships, the pipeline splits authorship from integration:
several "worker" sessions produce changes and open PRs; one "guardian" session (`master`) is the
sole path to `main` and to production. It is four-eyes review where the eyes are distinct,
role-scoped Claude Code sessions plus the CI gate stack. **Merge ≠ deploy ≠ Done.**

---

## 1. The guardian / integration authority (`master` session)

`master` is the **only session that touches `main` or production.** Remit:

- **Merge authority** — reviews each PR (correctness + security + coding-standards + doc-drift),
  then merges server-side. Workers never merge.
- **Deploy authority** — owns the deploy path (VPS gateway rebuild), live health verification,
  and rollback. A perfect plan with prod down is still a failure.
- **Proof enforcement** — "Done" means *proven against the backing ADR's acceptance criteria*,
  not "merged and runs." Evidence before assertion.
- **Drift catcher** — reconciles three things that lie independently: the code, the MASTER_PLAN,
  and the Linear board. State is verified against durable evidence (merged SHAs, CI results, live
  health), never against a label.
- **Deploy-class gating** — a standing owner-approved allowlist of low-risk, reversible deploys
  (PWA rebuild · additive ES-template · Kibana dashboard import) runs without asking. **Everything
  else** (gateway rebuild, ES type-change/reindex, Postgres schema/migration, cost/budget/governance)
  is **ask-first**, even after a PR is approved. Approving a fix ≠ authorizing a deploy.
- **PR-gate loop** — a 10-minute cron polls open PRs so nothing sits unreviewed; it surfaces a merge
  recommendation but is hard-blocked from merging/deploying without explicit owner go.

**Enforcement honesty (for the threat model):** the merge/deploy boundary is enforced primarily by
**session-role discipline + sole custody of the deploy path**, backed by the **CI gate stack** (§5).
GitHub **branch-protection could not be verified** from the guardian's token (403 on the protection
API — least-privilege is working, but the control is therefore unauditable from that seat). Treat
"is branch protection on and matching the CI gates?" as an open audit item (§7).

---

## 2. The three long-running worktrees — the authors

Three worker sessions run in parallel, each in its **own git worktree** on its **own branch**, but
**sharing one VPS working tree and one Docker stack**:

| Session | Worktree / branch prefix | Domain | Model |
|---|---|---|---|
| **build 1** | `worktrees/build` · `fre-<n>-*` | backend / Elasticsearch / gateway / memory | Opus |
| **build 2** | `worktrees/build2` · `fre-<n>-*` | config / PWA / memory / cross-cutting | Opus |
| **adr** | `worktrees/adrs` · `adr-*` | architecture decision records (docs only) | Opus |

Rules that keep them safe on a shared tree:

- **They self-dispatch** — each pulls its NEXT ticket from the MASTER_PLAN Stream Board
  (`/build 1`, `/build 2`, `/adr`) and drives its own TDD → plan → PR cycle.
- **They stop at "push branch + open PR."** They never merge, deploy, close tickets, or edit
  MASTER_PLAN. That is the hard session boundary.
- **File-domain partition** (backend vs PWA vs docs) so two streams don't edit the same file on the
  shared tree; genuinely cross-cutting tickets route through `master` to serialize.
- **One `pytest` at a time** — a pre-tool hook enforces a global test lock (the full suite is ~7 min;
  parallel runs saturate the 8-vCPU VPS).
- Merges happen **server-side from the primary tree**; deploys go **one at a time** from `main`.

Net effect: up to three change-streams in flight, converging through a single reviewed, gated merge point.

---

## 3. Relationship with Linear — the work-item control plane

**Linear (team `FrenchForest`) is the source of truth for *work items*.** The state machine is the
approval gate:

```
Needs Approval ──(owner approves)──► Approved ──(a worker starts)──► In Progress
   (New)                              (implementable)                 (≤1 per stream)
                                                                          │
                                              merged, awaiting my         ▼
   Done ◄──(master deploys + verifies live)── In Review ◄──────────── (PR opened→merged)
```

Control properties:

- **"New == Needs Approval. Implement == Approved."** Workers may only pull `Approved` tickets.
  **The owner owns the Approved gate** — no agent self-approves work into the build queue.
- `master` **verifies `Approved` via the API before honoring any implementation**, and reads the
  ticket comment thread at every gate (the decision trail lives there, not in the PR body).
- **Tier labels** (`Tier-1:Opus` / `Tier-2:Sonnet` / `Tier-3:Haiku`) drive per-ticket model routing.
- **Known automation trap:** merging a `fre-XXX`-branched PR **auto-moves the ticket to Done** via
  Linear's GitHub link. For a deploy-hold ticket that is a **false Done** — it bypasses the
  deploy+verify gate. `master` detects these and reopens them to `In Review` until actually deployed
  and verified. *Merge is not shipment.*
- **The board must not lie** — deferred/parked work is marked as such, never Done; multi-phase
  tickets stay In Progress until the last phase.

---

## 4. Relationship with the MASTER_PLAN — the priority/sequencing control plane

If Linear is *what* the work is, **MASTER_PLAN.md is *when* and *in what order*.** It lives in-repo,
**committed to `main` only** (never a feature branch), and is the guardian's to keep true.

- The **Stream Board** at its top is the live dispatch surface — a table (inside machine-parsed
  `<!-- STREAM-BOARD -->` markers) giving each stream its NEXT ticket + a context flag. The `/build`
  and `/adr` skills parse it; `master` advances it at every merge.
- It carries the **cross-project priority order** and wave sequencing (foundation-first, L0→L3),
  plus which work is parked/held and why.
- It deliberately **does not re-enumerate Linear** — duplicating per-ticket state is how it rots.
  (It was reduced 96k→11k on 2026-07-04 for exactly this reason; the bloat was stale per-ticket
  tables duplicating Linear + git history.)
- Bugs that put wrong data in front of the owner jump the queue regardless of layer; every
  merge/dispatch is weighed by **blast-radius × reversibility × gate-class.**

---

## 5. The CI/CD gate stack — what actually enforces quality

Every PR runs the following; **all must be green before `master` merges** (observed set):

- **CodeQL** + **Analyze** (python / javascript-typescript / actions) — SAST. A live control, not
  decorative: it caught a high-severity clear-text-credential finding that a "checks green" read
  would have missed (CodeQL alerts surface on the aggregate check-run, not the per-language Analyze
  jobs — a genuine trap).
- **Backend unit tests** + **integration tests (transport)**
- **Lint (mypy + ruff)** — types and style
- **PWA e2e (Playwright)** + **PWA unit (Vitest)**
- **Telemetry surface reconciliation gate** — project-specific: emit ↔ mapping ↔ dashboard consistency.

**Pre-commit hooks (local, pre-CI):**
- `check_no_personal_paths.py` — blocks machine-specific absolute paths (leak prevention).
- `check_no_direct_substrate_in_tests.py` — blocks tests writing to prod Neo4j/ES/Postgres
  (test-substrate isolation).
- ADR-0074 identity-threading check — enforces trace/identity propagation on log / bus / Cypher writes.

**Supply chain:** Dependabot is on. Standing item: open vulns on `main` are tracked and triaged
(as of this writing: 1 high + 1 moderate, pre-existing — see §7).

**Runtime security posture:** artifact/code execution is **sandboxed, not sanitized** (sealed-box
model, ADR-0089); config flows through a single validated settings object (no ad-hoc `os.getenv`);
secrets are never logged.

---

## 6. Topology at a glance

```
 Owner (approves tickets, authorizes ask-class deploys)
   │
   │  Linear: Needs Approval → Approved          MASTER_PLAN Stream Board
   ▼                    │                                   │ (priorities/sequencing)
 ┌───────────── worker sessions (authors, PR-only) ─────────────┐
 │  build1 (backend)   build2 (config/PWA)   adr (docs)         │
 │  worktree+branch     worktree+branch      worktree+branch    │
 └───────────────┬───────────────┬──────────────┬──────────────┘
                 │  push branch + open PR        │
                 ▼                               ▼
        GitHub PR  ──►  CI gate stack (CodeQL, tests, lint, e2e, recon)
                 │                               │ green?
                 ▼                               ▼
        ┌────────────────── master (guardian) ──────────────────┐
        │  review (code+security+doc-drift) → merge server-side │
        │  → ask/authorize deploy by class → deploy → verify    │
        │  live health → close Linear (proof) → advance PLAN    │
        └───────────────────────────────────────────────────────┘
                 │
                 ▼
        VPS repo root · gateway (cloud) · Postgres / ES / Neo4j / Redis
```

---

## 7. Where to focus a DevSecOps review

Honest gaps and items worth external eyes, in priority order:

1. **Branch protection is unverified** — the strongest boundary (workers *cannot* push/merge to
   `main`) may be procedural rather than technical. Confirm required-status-checks + restricted-push
   on `main` match the CI gates in §5. The guardian is currently blocked from reading it by token
   scope — itself a finding: least-privilege works, but the control is unauditable from that seat.
2. **Merge ≠ deploy ≠ Done** — the Linear auto-close-on-merge creates false "Done" signals for
   deploy-hold tickets. The guardian compensates manually; a hard rule (disable auto-close, or gate
   Done on a deploy event) would remove the human-in-the-loop dependency.
3. **Shared mutable substrate** — three worktrees share one VPS tree + one Docker stack. Isolation is
   by file-domain convention + a pytest lock, not by container/tenant boundaries. Acceptable for a
   single-owner research system; name it explicitly if it scales.
4. **Standing supply-chain debt** — open Dependabot alerts on `main` should be triaged into a tracked
   ticket rather than living as a repo banner.

Everything above is verifiable: git history for merges, Linear for ticket state, the ADR files for
design status, and GitHub Actions for gate results.
