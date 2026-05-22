---
name: ship-ticket
description: Use when shipping a Linear FRE ticket end-to-end — plan, implement, PR, deploy, verify, close. Encodes the project's full ticket-shipping loop with PR-hygiene, identity-threading, and verification gates.
---

# Ship Linear Ticket

End-to-end workflow for shipping a Linear ticket on this project. Argument: a Linear issue ID (e.g. `FRE-371`) OR omitted (then pick the top Approved ticket from MASTER_PLAN).

## Pre-flight (BEFORE writing any code)

1. **Verify ticket state** — `get_issue(<id>)` on FrenchForest team; must be `Approved`. If `Needs Approval`, stop and tell user.
2. **Report worktree/branch state** — `git worktree list` and `git branch --show-current`; paste output. If unexpected worktree exists, pause and confirm.
3. **Read context** — ticket body, linked ADRs, linked specs. Summarize scope in 3-5 bullets.
4. **Check phase boundaries** — if the linked ADR breaks the work into phases (ADR-0074 has 5), one phase = one PR. Do not bundle phases.
5. **Propose a plan** — atomic steps, exact file paths, exact test commands. Get explicit user approval. Do not skip even on small tickets.

## Implementation

6. **TDD** — write the failing test first; run it; confirm it fails as expected. Then implement.
7. **Standards check:**
   - Google docstrings, modern type hints (`str | None`), `settings.<field>` (never `os.getenv`), no bare `except:`, no `Any`.
   - **Identity threading (ADR-0074):** every new `log.*`, `bus.publish`, or Cypher `MERGE`/`CREATE` site carries `session_id` + `trace_id` from `TraceContext`. Use `# trace-allow: <reason>` only for boot/scheduler/monitor paths.
8. **Quality gates** — all must pass before PR:
   - `make test` (relevant module first, then full)
   - `make mypy`
   - `make ruff-check` + `make ruff-format`
   - `pre-commit run --all-files` (catches identity-threading, substrate-isolation, personal-path lints)

## PR (pre-merge only)

9. **Open PR** using `.github/PULL_REQUEST_TEMPLATE.md`. Checklist contains ONLY pre-merge items. **Forbidden in checklist:** prod verification, telemetry checks, deploy steps, "verify on prod after merge". Those go in a Linear comment.

10. **Address review feedback** rigorously — see superpowers:receiving-code-review.

## Post-merge (same session as merge — never deferred)

11. **Deploy** — `make deploy` (or env-specific target). Capture deploy output.
12. **Live verification** — `curl` the relevant prod endpoint; paste status + response body. Do not claim done from "deploy exited 0" alone.
13. **Conditional probe** — if the ticket touched a telemetry emit site, schema, cost recording, or memory write, run `scripts/monitors/joinability_probe.py` against prod and paste output (ADR-0074 §3.4).
14. **Telemetry check** — confirm new log fields / ES events / metrics appear if applicable.
15. **Update MASTER_PLAN.md on `main`** (not feature branch); commit + push.
16. **Close Linear ticket** with: PR link, deploy timestamp, verification evidence (curl/probe output snippet).

## Halt conditions

Stop and surface to the user — do not silently work around — if:
- Ticket is not `Approved`.
- Pre-existing worktree is on an unexpected branch.
- Plan would bundle multiple ADR phases into one PR.
- Plan involves dropping/quarantining historical rows — surface the row count and get explicit user confirmation (same shape as budget-change rule).
- `make mypy` shows >5 errors you didn't introduce (likely main-green issue; separate ticket).
- Deploy succeeds but live endpoint returns wrong response — file a follow-up Linear issue; do not mark current done.
- Joinability probe finds orphans — do not mark done; file a follow-up.
- Same error recurs after 3 fix attempts — escalate context per MODEL_ROUTING_POLICY.
