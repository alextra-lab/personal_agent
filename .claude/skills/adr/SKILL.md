---
name: adr
description: Use in the adr session (Opus) to explore an idea with the owner and — if it earns it — produce a complete ADR. Discuss/explore first, write, iterate with codex review, open ADR PR, then file sequenced implementation tickets. An ADR is a possible outcome, not a mandate. Never touches src/ or merges.
---

# ADR session — explore, then (if earned) author (always Opus)

Read `.claude/skills/lifecycle-rules.md` first. Confirm the session model is Opus; if not, STOP
(ADR authoring is Opus-only).

**Argument: none → resolve NEXT via the external dispatch resolver.** FIRST `git fetch origin`,
then run `python -m scripts.dispatch.next_resolver --stream adr --json`. A nonzero exit,
invalid JSON, or a printed error → STOP and surface stderr — never reconstruct the busy-guard /
priority / blocked-by logic inline. A `null` result (stream occupied, or no eligible candidate)
→ STOP. A non-null result names the ticket; honor its context flag exactly as `/build` does
(CLEAR default → must be a blank session; `context:keep` → proceed warm). An explicit `FRE-…`
id overrides the queue.

## Step 0 — Fresh-start (worktree reset)
1. `git fetch origin`
2. Safety gate — BOTH must hold, else STOP: `git status --short` empty, and the current branch
   merged (or nothing unpushed).
3. `git switch -c <next-adr-slug> origin/main`; retire the merged branch local-then-remote
   (`git branch -d` first — it refuses on unmerged — then `git push origin --delete`).

## 1 — Discuss / explore FIRST — genuinely, with the owner (hard gate)

**This is the load-bearing step.** Two modes — sense which you're in:
- **Assemble** — the decision already lives in the ticket; piece the ADR together. Discussion
  is lighter but still real.
- **Explore** — a half-formed idea develops — *or doesn't* — into an ADR. Explore + teach +
  ideate as a peer and a tutor; the exploration itself is the value. An ADR · a research note ·
  a parked idea · "not ADR-worthy yet" are ALL valid outcomes.

FORBIDDEN: *read the ticket → ask 2–3 clarifying questions → write the ADR and open the PR*
(the FRE-809 failure). Lay out the decision and its real tension, weigh alternatives out loud,
expect multiple rounds, and **write no file and open no PR until the owner explicitly says go**
("write it up", "draft it"). Unsure whether they said go → ask.

ADR work is Linear-tracked like build: set the umbrella ticket → `In Progress` at pickup (file
it first if this is ad-hoc work).

## 1.5 — Done = master-ready (responding to a poke)
**Never arm a `/loop` monitor and never poll CI** — the watcher covers it. Open the PR, go
idle; you are done when it is **master-ready** (any bounce resolved). On a poke (master bounce,
or watcher red-check): fix on this branch, push, go idle again.

## 2 — Write the ADR
Number it authoritatively — `git fetch origin && python scripts/next_adr.py --next` (never
eyeball `ls`; that caused the ADR-0117 double-number). Add the row to
`docs/architecture_decisions/README.md` in the same commit (the `check-adr-index` hook enforces
it). Start from `ADR_TEMPLATE.md`: References as a bulleted list, keep your own Status line
current, **≥2 Alternatives Considered** with why-rejected.

**Every ADR carries a Verification / Acceptance-Criteria section** — testable, discriminating
invariants at the **outcome** altitude, each stating how it is checked with existing
instrumentation. **No-BS bar: the criterion must be able to fail** — reject existence-checks
standing in for behaviour, "tests pass" with no test asserting the invariant, vanity counts,
and restatements of the task. If a criterion genuinely can't be made checkable, say so — that
is a design smell to surface, not paper over.

## 3 — Codex iterative review
`codex:rescue` on the ADR; revise per findings; repeat until no blocking findings, max 3
rounds; log each round in the PR description. Codex must explicitly check the criteria are
testable and outcome-level.

## 3.5 — Code-review obligation when the diff carries executable code
Any diff file outside `docs/architecture_decisions/**/*.md` that is a runnable artifact
(script, SQL migration, CI workflow, notebook, anything under `scripts/`/`sandbox/`/`src/`)
carries build's review obligations: commit first, run `feature-dev:code-reviewer` +
`security-review` on `git diff origin/main...HEAD`, fix confirmed findings on-branch, and apply
build's diff-class triggers (escalate → flag for owner `/code-review ultra` in the PR body +
handoff). A fenced code block inside the ADR's own markdown does not count; ambiguous → treat
as executable.

## 4 — PR
`git fetch origin && git rebase origin/main`, resolve conflicts in-session, push, open the ADR
PR (docs). Pre-merge checklist only.

## 5 — Implementation tickets
File the implementation tickets in Linear: `Needs Approval`, under a Linear project, sequenced
with `blockedBy` relations. Each ticket gets **acceptance criteria written for its own work** —
observable results of that ticket's change, decidable from its own deliverable, same no-BS bar.
Sanity-check the chain covers the ADR's Decision-section obligations — an obligation no ticket
delivers is a gap to fix at authoring time (file the missing ticket, or note it on the
umbrella). Where an obligation names two parties (A ships to B), assign it to whoever
**provisions** the thing, not the grammatical subject (the FRE-1220 lesson).

## 6 — Handoff comment for master — then STOP
Post a final comment on the ADR's Linear ticket addressed to master (required):
- intended ADR **status** on merge (Proposed / Accepted / Implemented) and any status-field change;
- implementation tickets filed + their sequence/dependencies;
- any doc-drift master should reconcile (related ADRs, CLAUDE.md, a skill contract);
- if the diff carried executable code (§3.5): the same self-review summary build reports;
- context disposition — keep or clear, and why.

**STOP. Never edit `src/`, never merge, never deploy, never write the owner console, never
mutate Linear control-plane fields** beyond your own pickup move. Filing tickets and posting
comments stay open to you.
