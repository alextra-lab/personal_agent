---
name: build
description: Use in the build session to ship a Linear FRE ticket from Approved to a master-ready PR (CI green + any bounce resolved) — fresh-start reset, plan with risk-tiered codex review (reviewer, not co-author), TDD, docs, PR, then self-complete via watcher/master pokes. Never merges or deploys.
---

# Build a Linear Ticket (build session)

Read `.claude/skills/lifecycle-rules.md` first. Argument: **a stream selector** (`1` or `2`), or an explicit Linear issue ID (e.g. `FRE-471`).

**Stream selector (`1`/`2`) → resolve NEXT via the external dispatch resolver** (`scripts/dispatch/next_resolver.py`, FRE-785; the busy guard, priority ordering, and blocked-by skip are NOT logic held in this skill — ADR-0113 §1). **FIRST `git fetch origin`** (you still need latest main for Step 0). Then run `python -m scripts.dispatch.next_resolver --stream build<N> --json`. A nonzero exit, invalid JSON, or a printed error (e.g. missing `AGENT_LINEAR_API_KEY`) → STOP and surface stderr — never fall back to reconstructing the busy-guard/priority/blocked-by logic inline. A `null` result (the stream is occupied — building, or a PR at master's gate that could bounce back; it frees at `Awaiting Deploy` — OR there is no eligible `Approved` candidate; the resolver conflates both) → STOP, same as the empty/ambiguous-queue case below. A non-null result names the ticket to build — read its `context:keep` status straight off the JSON's `labels` field (Step 1's `get_issue` still fetches the full ticket for scope/body). Honor its **context flag** (`context:keep` label → KEEP; absent → CLEAR):
- **CLEAR** (the default): this ticket wants a fresh slate. **First check: are you a blank/new session** — freshly started or just `/clear`ed, with essentially nothing in context but this invocation and session-start priming? If **yes**, proceed. If **no** (you still carry a previous ticket's work in context), STOP and tell the owner: "FRE-… is flagged CLEAR — run `/clear`, then `/build <N>` again." A stale prior-ticket context pollutes the plan.
- **KEEP**: the NEXT ticket is a direct follow-on (same files/substrate) — proceed on the current warm context regardless of the blank-session check; do not ask for a `/clear`.

An explicit `FRE-…` id skips the queue and builds that ticket (treat Context as CLEAR unless the owner says otherwise). If the queue is empty or ambiguous, STOP and ask master.

## Step 0 — Fresh-start (worktree reset + retire the merged branch)
1. `git fetch --prune origin`
2. Safety gate — BOTH must hold, else STOP and surface:
   - `git status --short` is empty
   - the current per-ticket branch is merged (or nothing unpushed: `git rev-list --count @{u}..HEAD` is `0`)
3. Cut a fresh branch off latest main for the new ticket: `git switch -c fre-<id>-<slug> origin/main`.
4. **Retire the now-merged previous branch — local THEN remote** (so branches don't pile up on origin).
   The lowercase `-d` is the verification: it refuses on an unmerged branch, so only a merged branch is
   ever deleted; run the remote delete only after `-d` succeeds. **Never** delete the
   `worktree-build` / `worktree-build2` / `worktree-adrs` anchors.
   - `git branch -d <merged-branch>`
   - `git push origin --delete <merged-branch>`
5. Confirm branch + worktree (`git worktree list`, `git branch --show-current`); paste.

## 1 — Ticket
`get_issue(<id>)` on FrenchForest; must be `Approved`. If `Needs Approval`, STOP and tell the owner.
Then **set the ticket → In Progress** (`save_issue state="In Progress"`). The GitHub integration
automates only the PR transitions (PR opened → `In Review`, PR merged → `Awaiting Deploy` — retargeted
2026-07-04); the session doing the work owns the In Progress transition; master owns the Done
transition at the gate.

## 1.5 — Done = master-ready (responding to a poke)
**Never poll CI — by any mechanism.** Not a `/loop`, not a background shell loop, not a Monitor, not a
`gh pr checks` retry: the prohibition is on the *behaviour*, not on one tool.

**The reason is redundancy, not cost** — be precise about this, because the wrong reason is the one that
gets argued away. The watcher already covers **both** directions and is verified live: it triggered master
four times on 2026-07-30 alone (`gating_send command='/master <PR>' reason=master-ready`) and pokes the
owning seat when CI goes red. A seat that spawns its own CI watch is duplicating a working sensor. (A
`/loop` carries an *additional* cost — it re-invokes the model every tick and blows the prompt-cache TTL,
which is why it was removed 2026-07-06 — but a background shell or Monitor does not, so do not rely on
that argument for them. They are still banned; the reason is that the watcher has it.)

**This does not apply to master's own PRs.** The watcher routes red CI by stream label to an owning
worker, and a master-authored PR has none — so master watches its own directly (lifecycle-rules
§ Master's own PRs have no safety net). That asymmetry is deliberate, not an exemption you inherit.

You do the work and open the PR, then go idle — but **you are not done at the PR; you are done when the PR
is master-ready: CI green AND any bounce resolved.** You do not *wait* for CI. You go idle, and something
re-engages this warm seat:
- **CI goes red** → the **watcher** pokes this seat with a plain message.
- **master bounces** → **master** tells this seat directly.

Either poke: read it, fix on this branch, re-run the Step-8 quality gates, `git push`, go idle again. Your
context is warm — you built this — so the fix is cheap. *(This is the old `/prime-worker`, folded in; that
skill is gone.)*

## 2 — Scope
Read ticket body + linked ADRs + specs. Summarize scope in 3–5 bullets.

**Read the backing ADR for design intent — not for criteria to inherit** (ADR-0130 D1/D3). The diff
must implement that ADR *as designed*, and silent divergence still bounces at the gate; but the ADR's
own acceptance criteria are **not yours to carry, quote, restate or discharge** — they are asserted
once, by that ADR's seam ticket (lifecycle-rules § Ticket state › Seam tickets).

**Pull out the acceptance criteria written on this ticket** — the testable, outcome-level invariants
about *this ticket's own change*, decidable from your own deliverable once you are finished. They are
the definition of done. If a feature ticket names none and it is not a standalone bug, **flag the gap
before coding** — master will bounce a PR with no provable criteria, and criteria lifted from the
backing ADR are not a substitute; ask master to have criteria written on the ticket instead.

## 3 — Plan + (risk-tiered) codex review
Write a plan: atomic steps, exact file paths, exact test commands.

**Self-classify the work from the Step-2 scope (you have the most context here — master does not pre-route this):**
- **Trivial** — mechanical only: docs / config / test-only / a one-liner; **no `src/` logic change, no
  schema / security / cost / memory / new-ADR-implementation**. → **skip codex plan-review**; the Approved
  ticket is sufficient authorization, proceed straight to TDD.
- **Standard / Complex** — touches `src/` logic, schema, security, cost, memory, a new ADR's
  implementation, or multi-file behavior. → **codex plan-review REQUIRED**: invoke **codex:rescue** on the
  plan (approach second-opinion), revise per findings, and get explicit owner approval before coding.

**When in doubt, treat as Standard and run codex** — bias toward review. **Codex is a reviewer here, not a
co-author:** you own the work and codex gives the independent, adversarial second opinion (adversarial beats
redundant); reach for codex as a *collaborator* only in Step 7 rescue, when you're genuinely stuck. Lean
reviewer — that's your judgment to make per plan. The owner/master may override per ticket in the dispatch
with `[codex: required]` (force it) or `[codex: skip]` (force-skip) when they know something the scope
doesn't show. Master backstops this at the gate — a mis-tiered Standard change that
skipped codex gets bounced. (One phase = one PR — see halt conditions.)

## 4 — TDD implement
Failing test first → confirm it fails → implement. **Each of this ticket's own acceptance criteria
from Step 2 gets a test or probe that asserts the *outcome* — the invariant actually holds — not that
the component is wired; this is the proof master's gate reads.** Standards (`.claude/CLAUDE.md`) +
ADR-0074 identity threading on every new `log.*` / `bus.publish` / Cypher `MERGE|CREATE`.

## 5 — Meet the objective — fold in, don't over-ticket
A ticket is an **objective** — a user story, usually spawned from an ADR but not uniquely — **not a
boxed single change with no deviation allowed.** Your job is to **meet the objective**; the supporting
changes and reasonable deviations you discover while building or in review are *part of meeting it*,
not separate work. This is a single-developer project — tickets are for **structured sequencing** of
real / planned / ADR work, not a log of every change; over-ticketing only delays development.
- **Non-ADR supporting changes needed to make THIS build function** (a helper, a small fix, a config
  tweak the feature depends on) → **fold them into this PR.** Do NOT file a ticket. Note them in the
  handoff (Step 9) so master validates them as supporting rather than reading them as scope creep.
- **Findings from a code review — your self-review (Step 8) or master's gate — that are reasonable
  changes to THIS build task** → fix them **in this PR.** No multi-ticket paper trail for review fixes.
- File a **new Needs-Approval ticket ONLY** for genuinely separate, sequenceable work OR anything
  **ADR-requiring** (a design decision / a new architectural surface). When unsure, **prefer folding
  in over ticketing** — a ticket explosion buys nothing for one developer.

## 6 — Documentation
Update docs the change touches (skill docs, READMEs, doc-strings).

## 7 — Codex rescue (escalation only)
3 failed attempts OR same error twice OR self-revert → invoke **codex:rescue** with full error context.

## 8 — Quality gates (all pass before PR)
`make test` (module then full) · `make mypy` · `make ruff-check` + `make ruff-format` ·
`pre-commit run --all-files`.

**Self-review before the PR (shift-left — you fix your own findings so master never has to bounce
them).** Run it **once, at this pre-PR gate — NOT on every implementation turn** (a strategic
checkpoint, not a per-turn tax).

**Commit first.** Both reviewers below diff the **committed branch against `main`** — uncommitted
work is invisible to them and reads back as a clean result from an empty diff, which is a
silent-pass, not a real review (found live 2026-08-06, FRE-1128). Commit locally to this branch
before invoking either; no PR is required for them to see it.

The bare `code-review` skill is blocked for a working session (`disable-model-invocation`), and
`code-review:code-review` (the plugin) is PR-shaped — it fetches a diff by PR number and comments
back on the PR, and at this step no PR exists yet. Use the two reviewers a build session can
actually invoke unassisted, both scoped explicitly to `git diff origin/main...HEAD` (the merge-base
diff — matches what the eventual PR diff shows; **`feature-dev:code-reviewer`'s own default is
unstaged `git diff`, which is empty once you've committed** — say the range in the prompt, don't
rely on the default):
- **`feature-dev:code-reviewer`** (Agent tool, `subagent_type: "feature-dev:code-reviewer"`) — reviews
  the diff against this project's CLAUDE.md standards; reports confirmed bugs/quality issues by
  severity (Critical / Important).
- **`security-review`** — invoke when the diff touches inputs / subprocess / files / auth / secrets /
  network.

**Fix every confirmed finding on your branch before opening the PR.**

**Route the obligation by diff class, not uniformly** — a twenty-line helper otherwise costs the same
interruption as a sixteen-hundred-line write-path change:
- **Self-serve (default)** — nothing below applies: docs, config, tests, process/skill wording
  (including this ticket's own diff), dev/sandbox tooling that never touches production data,
  read-only code. Run both reviewers yourself; fix confirmed findings on-branch; done.
- **Escalate — if ANY of these apply, it escalates** (precedence: escalation wins on ambiguity;
  these cover the *code* path, not process-doc wording — a skill/lifecycle-rules edit stays
  self-serve unless it changes the trust ladder in `docs/plans/OWNER_CONSOLE.md`, which already has
  its own gate):
  1. **Production write path** — issues create/update/delete/merge against Neo4j, Postgres,
     Elasticsearch, or R2 in the running service, **or sits directly in that write's call chain**
     (a helper/serializer/validator feeding an existing write) — even if that diff alone looks small.
  2. **Destructive or deleting** — any code path capable of deleting or evicting data.
  3. **Schema change** — DB / ES / graph schema, a migration, or a type change.
  4. **Cost or governance code** — modifies `cost_gate`, budget enforcement, deploy-authorization
     logic, or model-routing policy code.

  On escalate: self-serve review still runs and still gets fixed on-branch, **and** note in the PR
  body + ticket handoff "diff class: escalated — flagged for owner `/code-review ultra` before
  merge" — a flag, not a wait; you still go idle after the PR per § 1.5, master raises it with the
  owner at the gate.
  - *Worked:* the FRE-1115 diff (1,600 insertions, a deleting script, a production write path) →
    escalates on triggers 1+2. A docs-only or small-config diff, or this ticket's own skill-wording
    diff → self-serves. A small helper that reformats a payload immediately before an existing
    Postgres write call → escalates on trigger 1, even though the diff itself is tiny — it's in the
    write's call chain.

(FRE-847: a review pass caught 3 confirmed correctness bugs in a 146-line script — "it's small" is
grounds to self-serve, never to skip review outright.)

## 9 — PR + final ticket comment for master — then STOP
**Sync to latest main FIRST** (prevents a stale-base collision / a DIRTY PR at master's gate when a
sibling PR merged during your session): `git fetch origin && git rebase origin/main` — resolve any
conflicts **in-session** (you have the context; master won't), re-run the Step 8 quality gates, then
`git push --force-with-lease`. Then open the PR with `.github/PULL_REQUEST_TEMPLATE.md`. Pre-merge
checklist ONLY (see lifecycle-rules PR hygiene).

**Then post a final comment on the Linear ticket addressed to master** (`save_comment` on the
issue) — this is required, not optional. It carries everything master needs that does NOT belong
in the PR's pre-merge checklist:
- **acceptance-criteria proof** (the master gate's input — master SKILL Step 4): the backing ADR named
  for **provenance and design adherence**, then **this ticket's own acceptance criteria** and, for each,
  the evidence it is delivered end to end (test name + result, probe/query output, or observed behaviour
  at the criterion's altitude). Record the **observed value**, not the assertion that you checked it.
  Without it master bounces the PR. The backing ADR's *own* criteria are **not** proven here — they
  belong to its seam ticket (ADR-0130 D2). *(Standalone bug: the reproducing test / verification stands
  in for ADR provenance.)*
- **self-review summary** (the executive input for master's gate — master SKILL Step 2): the
  **diff class** (self-serve / escalated — and, if escalated, whether the owner already ran
  `/code-review ultra` and its outcome) **and** the security-review verdict; what the reviews
  flagged (confirmed findings, most-severe first) and what you fixed on-branch; call out anything
  you deliberately did NOT fix and why. "No findings" is a valid summary. A real-logic diff with no
  summary gets bounced.
- the **post-deploy runbook** (exact ES/Kibana/migration/verification steps, in order);
- any **safety constraints / gotchas** (e.g. "do NOT back-attach existing indices", "register the
  template before first write", "verify the code is generating the logs");
- **what to verify live** to prove the AC (commands + expected output);
- discovered follow-up tickets filed;
- the Linear auto-Done caveat if the deploy will be batched;
- **your context disposition for the next ticket** — whether you want your context **kept** (the next
  queued ticket is a direct follow-on — same files/feature, multi-phase, regression test for what you
  just built, depends on a fresh discovery) or **cleared** (`/clear` — different area; you know your own
  context best). State it plainly, e.g. "FRE-X next: keep — shares this refactor" / "clear before next".
Master reads this comment by default at the gate, so it is the handoff channel. **These fields are
the handoff contract** master trusts without re-deriving (lifecycle-rules § Signal trust boundary) —
fill every one; a real-logic diff whose handoff is missing per-AC evidence or the self-review summary
is bounced, not reconstructed by master.

**STOP. Do not merge, deploy, close the ticket, write the owner console, or mutate Linear
control-plane fields beyond the `In Progress` move you made at pickup** — that is master's role.
Filing tickets and posting comments stay open to you (lifecycle-rules § Coordination stores).
