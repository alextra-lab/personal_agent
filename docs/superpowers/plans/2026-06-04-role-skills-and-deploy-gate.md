# Role-Scoped Session Skills + Deploy-Approval Gate — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split the `ship-ticket` skill into role-scoped `/build`, `/master`, `/adr`, `/prime-master` skills over a shared `lifecycle-rules.md`, and add a `deploy-approval-gate.sh` hook that makes cross-role deploy prohibitions mechanical.

**Architecture:** Markdown skills carry per-role workflow and reference one shared invariants file. A single PreToolUse/Bash hook self-determines its session role from the worktree path (`git rev-parse --show-toplevel`) and hard-denies deploy commands in build/adr worktrees, sentinel-gates them in master. Follows the existing repo hook contract (exit 2 = block).

**Tech Stack:** Bash hooks (stdin JSON → exit 2 to block), Claude Code skills (`SKILL.md` + frontmatter), `.claude/settings.json` hook registration, `gh`/Linear MCP referenced by skills.

**Spec:** `docs/superpowers/specs/2026-06-04-role-skills-and-deploy-gate-design.md`

**Caveat for enforcement (read first):** The hook script + `settings.json` registration must reach the build and adr worktrees before the hook can fire there. Worktree branches are typically behind `main`; the Step-0 fresh-start (`git merge --ff-only origin/main`) syncs them. After merging this work, each session must sync its worktree once for enforcement to take effect.

---

## File Structure

| Path | Responsibility |
|------|----------------|
| `.claude/skills/lifecycle-rules.md` | Shared invariants (PR hygiene, session boundary, MASTER_PLAN rule, halt conditions). |
| `.claude/hooks/deploy-approval-gate.sh` | PreToolUse/Bash gate: role detection + deploy deny/allow. |
| `.claude/hooks/test_deploy_approval_gate.sh` | Bash test harness for the gate. |
| `.claude/skills/build/SKILL.md` | Build-session workflow. |
| `.claude/skills/master/SKILL.md` | Master-session workflow. |
| `.claude/skills/adr/SKILL.md` | ADR-session workflow (Opus). |
| `.claude/skills/prime-master/SKILL.md` | Guardian context reset. |
| `.claude/settings.json` | Register the new PreToolUse hook. |
| `.claude/skills/ship-ticket/` | Removed (content redistributed). |

---

## Task 1: Shared lifecycle-rules.md

**Files:**
- Create: `.claude/skills/lifecycle-rules.md`

- [ ] **Step 1: Write the shared invariants file**

Create `.claude/skills/lifecycle-rules.md` with this exact content:

```markdown
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
```

- [ ] **Step 2: Verify structure**

Run: `test -f .claude/skills/lifecycle-rules.md && grep -c '^## ' .claude/skills/lifecycle-rules.md`
Expected: prints `6` (six rule sections).

- [ ] **Step 3: Commit**

```bash
git add .claude/skills/lifecycle-rules.md
git commit -m "feat(skills): shared lifecycle-rules.md — single source of invariants"
```

---

## Task 2: deploy-approval-gate.sh hook (TDD)

**Files:**
- Create: `.claude/hooks/deploy-approval-gate.sh`
- Test: `.claude/hooks/test_deploy_approval_gate.sh`

- [ ] **Step 1: Write the failing test harness**

Create `.claude/hooks/test_deploy_approval_gate.sh` with this exact content:

```bash
#!/usr/bin/env bash
# Test harness for deploy-approval-gate.sh.
# Drives the hook with mock PreToolUse payloads from different CWDs and asserts
# exit codes. Never runs an actual deploy — the hook only gates.
set -uo pipefail

HOOK="$(cd "$(dirname "$0")" && pwd)/deploy-approval-gate.sh"
PRIMARY="/opt/seshat"
BUILD_WT="/opt/seshat/.claude/worktrees/build"
ADR_WT="/opt/seshat/.claude/worktrees/adrs"
SENTINEL="$PRIMARY/.claude/.deploy-approved"
fails=0

payload() { printf '{"tool_name":"Bash","tool_input":{"command":"%s"}}' "$1"; }

assert_exit() { # desc, expected_code, actual_code
  if [ "$2" = "$3" ]; then echo "ok   - $1"; else echo "FAIL - $1 (expected $2 got $3)"; fails=$((fails+1)); fi
}

# 1. Deploy command in build worktree → DENY (2)
( cd "$BUILD_WT" && payload "ENV=cloud make rebuild SERVICE=seshat-gateway" | bash "$HOOK" ); assert_exit "build worktree denies deploy" 2 $?
# 2. Deploy command in adr worktree → DENY (2)
( cd "$ADR_WT" && payload "make deploy" | bash "$HOOK" ); assert_exit "adr worktree denies deploy" 2 $?
# 3. Non-deploy command in build worktree → ALLOW (0)
( cd "$BUILD_WT" && payload "make test" | bash "$HOOK" ); assert_exit "build worktree allows non-deploy" 0 $?
# 4. Deploy in master, NO sentinel → DENY (2)
rm -f "$SENTINEL"
( cd "$PRIMARY" && payload "ENV=cloud make rebuild SERVICE=seshat-gateway" | bash "$HOOK" ); assert_exit "master denies deploy without sentinel" 2 $?
# 5. Deploy in master WITH fresh sentinel → ALLOW (0) and sentinel consumed
touch "$SENTINEL"
( cd "$PRIMARY" && payload "ENV=cloud make rebuild SERVICE=seshat-gateway" | bash "$HOOK" ); assert_exit "master allows deploy with sentinel" 0 $?
[ ! -f "$SENTINEL" ]; assert_exit "sentinel consumed after use" 0 $?
# 6. Non-deploy command in master → ALLOW (0)
( cd "$PRIMARY" && payload "git status" | bash "$HOOK" ); assert_exit "master allows non-deploy" 0 $?

echo "---"; [ "$fails" -eq 0 ] && echo "ALL PASS" || { echo "$fails FAILED"; exit 1; }
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `chmod +x .claude/hooks/test_deploy_approval_gate.sh && bash .claude/hooks/test_deploy_approval_gate.sh`
Expected: FAIL — hook does not exist yet (`deploy-approval-gate.sh: No such file`).

- [ ] **Step 3: Write the hook**

Create `.claude/hooks/deploy-approval-gate.sh` with this exact content:

```bash
#!/usr/bin/env bash
# PreToolUse hook: gate deploy commands by session role.
#   - build / adr worktrees: hard-deny any deploy command (those sessions never deploy).
#   - master (primary tree): deny unless a fresh approval sentinel exists; consume it on use.
# Role is determined by the worktree root of the hook's CWD.
# Exit 2 = block and surface the message (matches repo hook contract).
set -uo pipefail

input=$(cat)
cmd=$(printf '%s' "$input" | python3 -c "
import sys, json
try:
    print(json.load(sys.stdin).get('tool_input', {}).get('command', ''))
except Exception:
    pass
" 2>/dev/null)

# Only inspect deploy-class commands.
if ! printf '%s' "$cmd" | grep -qE '(ENV=cloud[[:space:]]+make|make[[:space:]]+(rebuild|deploy|build|build-full|tunnel-up))'; then
    exit 0
fi

root=$(git rev-parse --show-toplevel 2>/dev/null || pwd)
sentinel="$root/.claude/.deploy-approved"

case "$root" in
    */.claude/worktrees/build|*/.claude/worktrees/adrs)
        printf 'BLOCKED: deploy commands are forbidden in the build/adr session (role boundary). master deploys.\n'
        exit 2
        ;;
esac

# master / primary tree: require a fresh sentinel (< 5 min old), then consume it.
if [ -f "$sentinel" ]; then
    if [ -n "$(find "$sentinel" -mmin -5 2>/dev/null)" ]; then
        rm -f "$sentinel"
        exit 0
    fi
    rm -f "$sentinel"  # stale — drop it and fall through to deny
fi

printf 'BLOCKED: deploy requires explicit owner approval. The /master skill writes .claude/.deploy-approved only after you answer "deploy now? yes".\n'
exit 2
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `chmod +x .claude/hooks/deploy-approval-gate.sh && bash .claude/hooks/test_deploy_approval_gate.sh`
Expected: `ALL PASS`.

- [ ] **Step 5: Commit**

```bash
git add .claude/hooks/deploy-approval-gate.sh .claude/hooks/test_deploy_approval_gate.sh
git commit -m "feat(hooks): deploy-approval-gate — role-aware deploy deny/allow"
```

---

## Task 3: Register the hook in settings.json

**Files:**
- Modify: `.claude/settings.json` (PreToolUse Bash hooks array)

- [ ] **Step 1: Add the hook registration**

In `.claude/settings.json`, find the `PreToolUse` array's existing `"matcher": "Bash"` block (the one running `check-pytest-lock.sh`) and add the new hook to its `hooks` list so it reads:

```json
        {
          "matcher": "Bash",
          "hooks": [
            {
              "type": "command",
              "command": ".claude/hooks/check-pytest-lock.sh"
            },
            {
              "type": "command",
              "command": ".claude/hooks/deploy-approval-gate.sh"
            }
          ]
        }
```

- [ ] **Step 2: Verify JSON validity and registration**

Run: `python3 -c "import json; d=json.load(open('.claude/settings.json')); cmds=[h['command'] for m in d['hooks']['PreToolUse'] if m.get('matcher')=='Bash' for h in m['hooks']]; assert '.claude/hooks/deploy-approval-gate.sh' in cmds, cmds; print('registered:', cmds)"`
Expected: prints the command list including `deploy-approval-gate.sh`; no assertion error.

- [ ] **Step 3: Add the sentinel to .gitignore**

Run: `grep -qxF '.claude/.deploy-approved' .gitignore || printf '\n# deploy-approval-gate runtime sentinel (never committed)\n.claude/.deploy-approved\n' >> .gitignore`
Then verify: `grep -c '.claude/.deploy-approved' .gitignore`
Expected: prints `1`.

- [ ] **Step 4: Commit**

```bash
git add .claude/settings.json .gitignore
git commit -m "feat(hooks): register deploy-approval-gate; gitignore deploy sentinel"
```

---

## Task 4: /build skill

**Files:**
- Create: `.claude/skills/build/SKILL.md`

- [ ] **Step 1: Write the build skill**

Create `.claude/skills/build/SKILL.md` with this exact content:

````markdown
---
name: build
description: Use in the build session to ship a Linear FRE ticket from Approved to PR — fresh-start reset, plan with codex review, TDD, follow-up tickets, docs, PR. Stops at PR; never merges or deploys.
---

# Build a Linear Ticket (build session)

Read `.claude/skills/lifecycle-rules.md` first. Argument: a Linear issue ID (e.g. `FRE-471`), or omitted (pick the top Approved ticket from MASTER_PLAN).

## Step 0 — Fresh-start (worktree reset)
1. `git fetch origin`
2. Safety gate — BOTH must hold, else STOP and surface:
   - `git status --short` is empty
   - `git rev-list --count @{u}..HEAD` is `0` (nothing unpushed)
3. Sync the persistent branch: `git merge --ff-only origin/main` then `git push origin worktree-build`.
4. Confirm branch + worktree (`git worktree list`, `git branch --show-current`); paste.

## 1 — Ticket
`get_issue(<id>)` on FrenchForest; must be `Approved`. If `Needs Approval`, STOP and tell the owner.

## 2 — Scope
Read ticket body + linked ADRs + specs. Summarize scope in 3–5 bullets.

## 3 — Plan + codex review
Write a plan: atomic steps, exact file paths, exact test commands. Then invoke **codex:rescue**
to review the plan (approach second-opinion). Revise per findings. Get explicit owner approval
before coding. (One phase = one PR — see halt conditions.)

## 4 — TDD implement
Failing test first → confirm it fails → implement. Standards (`.claude/CLAUDE.md`) + ADR-0074
identity threading on every new `log.*` / `bus.publish` / Cypher `MERGE|CREATE`.

## 5 — Follow-up tickets
File any discovered work as new issues — Needs Approval, under a Linear project (default: the
project of the ticket being worked).

## 6 — Documentation
Update docs the change touches (skill docs, READMEs, doc-strings).

## 7 — Codex rescue (escalation only)
3 failed attempts OR same error twice OR self-revert → invoke **codex:rescue** with full error context.

## 8 — Quality gates (all pass before PR)
`make test` (module then full) · `make mypy` · `make ruff-check` + `make ruff-format` ·
`pre-commit run --all-files`.

## 9 — PR — then STOP
Open the PR with `.github/PULL_REQUEST_TEMPLATE.md`. Pre-merge checklist ONLY (see lifecycle-rules
PR hygiene). Push the branch. **STOP. Do not merge, deploy, close the ticket, or edit MASTER_PLAN** —
that is master's role.
````

- [ ] **Step 2: Verify structure**

Run: `grep -q '^name: build' .claude/skills/build/SKILL.md && grep -q 'lifecycle-rules.md' .claude/skills/build/SKILL.md && grep -q 'codex:rescue' .claude/skills/build/SKILL.md && echo OK`
Expected: `OK`.

- [ ] **Step 3: Commit**

```bash
git add .claude/skills/build/SKILL.md
git commit -m "feat(skills): /build — build-session ticket workflow (plan→codex→TDD→PR)"
```

---

## Task 5: /master skill

**Files:**
- Create: `.claude/skills/master/SKILL.md`

- [ ] **Step 1: Write the master skill**

Create `.claude/skills/master/SKILL.md` with this exact content:

````markdown
---
name: master
description: Use in the master session to integrate a ready PR — analyze (code-review + security-review), doc-drift check, merge, ask before deploy, verify live, close Linear, update MASTER_PLAN.
---

# Integrate a PR (master / guardian session)

Read `.claude/skills/lifecycle-rules.md` first. Argument: a PR number, or omitted (scan open PRs).

## 1 — Pick the PR
`gh pr list` (or use the given number). Read PR body, commits, linked ticket.

## 2 — Analyze the diff
- Correctness: invoke the **code-review** skill on the diff.
- Security: invoke the **security-review** skill on the diff (the pre-merge security pass).
Surface findings. Block merge on real issues; relay to the build session.

## 3 — Doc-drift check
Does this change require updates to MASTER_PLAN, `CLAUDE.md` "Current status", or an ADR status
field? Flag drift before merging. (Documentation-drift sensitivity is a core guardian duty.)

## 4 — Gate checks
Ticket is `Approved`/In Progress; PR hygiene holds (REJECT if post-deploy items are in the
checklist); CI green.

## 5 — Merge
`gh pr merge <n> --merge` with a review summary; `git pull` on main.

## 6 — Ask before deploy
Ask the owner: **"deploy now?"** Do NOT deploy on your own initiative. If yes, write the approval
sentinel so the gate allows exactly one deploy:
`touch .claude/.deploy-approved`

## 7 — Deploy + verify (only after "yes")
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
````

- [ ] **Step 2: Verify structure**

Run: `grep -q '^name: master' .claude/skills/master/SKILL.md && grep -q 'security-review' .claude/skills/master/SKILL.md && grep -q '.deploy-approved' .claude/skills/master/SKILL.md && grep -q 'deploy now' .claude/skills/master/SKILL.md && echo OK`
Expected: `OK`.

- [ ] **Step 3: Commit**

```bash
git add .claude/skills/master/SKILL.md
git commit -m "feat(skills): /master — PR integration (review+security→merge→ask-deploy→verify→close)"
```

---

## Task 6: /adr skill

**Files:**
- Create: `.claude/skills/adr/SKILL.md`

- [ ] **Step 1: Write the adr skill**

Create `.claude/skills/adr/SKILL.md` with this exact content:

````markdown
---
name: adr
description: Use in the adr session (Opus) to produce a complete ADR — discuss first, write, iterate with codex review, open ADR PR, then file sequenced implementation tickets. Never touches src/ or merges.
---

# Author an ADR (adr session — always Opus)

Read `.claude/skills/lifecycle-rules.md` first. Confirm the session model is Opus; if not, STOP
and tell the owner (ADR authoring is Opus-only).

## Step 0 — Fresh-start (worktree reset)
1. `git fetch origin`
2. Safety gate — BOTH must hold, else STOP and surface:
   - `git status --short` is empty
   - the current per-ADR branch is merged (or there is nothing unpushed: `git rev-list --count @{u}..HEAD` is `0`)
3. Cut a fresh branch off latest main: `git switch -c <next-adr-slug> origin/main`.
4. Retire the merged branch: `git branch -d <merged-adr-branch>` (lowercase `-d` refuses if unmerged).

## 1 — Discuss first
Collaborate with the owner on the decision. Do NOT write any file until the decision is settled
(discussion-mode default).

## 2 — Write the ADR
Author the best, complete ADR in the project ADR format under `docs/architecture_decisions/`.

## 3 — Codex iterative review
Invoke **codex:rescue** to review the ADR. Revise per findings. Repeat until no blocking findings,
**max 3 rounds**. Log each round's findings in the PR description.

## 4 — PR
Open the ADR PR (docs). Pre-merge checklist only.

## 5 — Implementation tickets
File the implementation tickets in Linear: Needs Approval, under a Linear project, sequenced with
dependencies. The owner approves → the build session picks them up.

## Boundary
Never edit `src/`, never merge, never deploy, never edit MASTER_PLAN.
````

- [ ] **Step 2: Verify structure**

Run: `grep -q '^name: adr' .claude/skills/adr/SKILL.md && grep -q 'lifecycle-rules.md' .claude/skills/adr/SKILL.md && grep -q 'max 3 rounds' .claude/skills/adr/SKILL.md && echo OK`
Expected: `OK`.

- [ ] **Step 3: Commit**

```bash
git add .claude/skills/adr/SKILL.md
git commit -m "feat(skills): /adr — Opus ADR authoring (discuss→write→codex×3→PR→tickets)"
```

---

## Task 7: /prime-master skill

**Files:**
- Create: `.claude/skills/prime-master/SKILL.md`

- [ ] **Step 1: Write the prime-master skill**

Create `.claude/skills/prime-master/SKILL.md` with this exact content:

````markdown
---
name: prime-master
description: Use after /clear in the master session to rebuild the guardian snapshot from durable sources (MEMORY, MASTER_PLAN, git, Linear, health) — never from prior conversation.
---

# Prime the Guardian Session

Read `.claude/skills/lifecycle-rules.md` first. Reconstruct the master snapshot from DURABLE
sources only — never from prior conversation context.

## Pre-reset safety gate (run before /clear, if winding down)
Only reset master context at a clean integration boundary — ALL must hold:
- Active Pending Verification: none.
- No PR mid-merge, no ticket half-closed.
- MASTER_PLAN ↔ Linear in sync (no undocumented status drift).
- Working tree clean on `main`.
If any fails: finish or record it (bump MASTER_PLAN "Last updated") before clearing.

## Rebuild snapshot (after /clear)
1. MEMORY.md is auto-loaded — standing rules apply.
2. Read MASTER_PLAN: header, "Last updated", Pending Verification, Needs Approval.
3. `git status` · `git worktree list` · `gh pr list` (open PRs awaiting master).
4. Linear: list In Progress + Pending Verification tickets on FrenchForest.
5. `curl -s http://localhost:9001/health` — live gateway health + note deployed SHA (`git log -1 --oneline`).

## Output
Print the guardian snapshot: current state · next-per-sequence · active pending verification ·
identity guardrails (never use injected userEmail; use owner test email). This is the re-prime block.
````

- [ ] **Step 2: Verify structure**

Run: `grep -q '^name: prime-master' .claude/skills/prime-master/SKILL.md && grep -q 'durable' .claude/skills/prime-master/SKILL.md && grep -q 'Pending Verification' .claude/skills/prime-master/SKILL.md && echo OK`
Expected: `OK`.

- [ ] **Step 3: Commit**

```bash
git add .claude/skills/prime-master/SKILL.md
git commit -m "feat(skills): /prime-master — guardian context reset from durable sources"
```

---

## Task 8: Retire ship-ticket + sweep references

**Files:**
- Remove: `.claude/skills/ship-ticket/`

- [ ] **Step 1: Find references to ship-ticket**

Run: `grep -rn 'ship-ticket' --exclude-dir=.git . | grep -v 'docs/superpowers/specs/2026-06-04' | grep -v 'docs/superpowers/plans/2026-06-04'`
Expected: lists any references outside this spec/plan. (Note them; the only expected hits are historical session logs, which are fine to leave.)

- [ ] **Step 2: Remove the skill**

Run: `git rm -r .claude/skills/ship-ticket/`
Expected: removes `SKILL.md` (and any sibling files).

- [ ] **Step 3: Verify removal**

Run: `test ! -d .claude/skills/ship-ticket && echo REMOVED`
Expected: `REMOVED`.

- [ ] **Step 4: Commit**

```bash
git commit -m "chore(skills): retire ship-ticket — split into /build + /master"
```

---

## Task 9: Final verification

- [ ] **Step 1: Run the hook test once more**

Run: `bash .claude/hooks/test_deploy_approval_gate.sh`
Expected: `ALL PASS`.

- [ ] **Step 2: Confirm all skills load structurally**

Run: `for s in build master adr prime-master; do grep -q "^name: $s" .claude/skills/$s/SKILL.md && grep -q lifecycle-rules .claude/skills/$s/SKILL.md && echo "$s OK" || echo "$s MISSING"; done`
Expected: `build OK` / `master OK` / `adr OK` / `prime-master OK`.

- [ ] **Step 3: Confirm settings.json is valid and hook registered**

Run: `python3 -c "import json; json.load(open('.claude/settings.json')); print('valid')"`
Expected: `valid`.

---

## Self-Review (completed by plan author)

**Spec coverage:** §3.1 shared rules → Task 1. §3.2 /build → Task 4. §3.3 /master → Task 5.
§3.4 /adr → Task 6. §3.5 deploy hook → Tasks 2–3. §3.6 /prime-master → Task 7. §4 reset patterns
→ Step-0 in Tasks 4/6 + pre-reset gate in Task 7. §5 security → Task 5 step 2. §6 file inventory →
Tasks 1–8. §8 defaults (3 rounds / project / 5-min TTL) → encoded in Tasks 6, 4, 2 respectively.

**Placeholder scan:** none — every file has full content; every command has expected output.

**Type/name consistency:** sentinel path `.claude/.deploy-approved` identical in hook (Task 2),
gitignore (Task 3), master skill (Task 5), and test (Task 2). Skill `name:` values match directory
names and the `/prime-master` references. Hook exit-2 contract matches existing repo hooks.

## Deployment note (post-merge, master)

This is a `.claude/` tooling change — no gateway rebuild needed. After merge, each session syncs its
worktree (Step-0 fresh-start) to receive the hook + settings; only then does the gate enforce in the
build/adr sessions.
