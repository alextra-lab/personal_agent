# Master Plan — History (grepable narrative)

> **This file is the append-only, grepable history of the project's decisions and shipped work.**
> It is **NOT** auto-loaded on re-prime — `MASTER_PLAN.md` (the concise plan) is. Search here when you
> need "when did X ship / what was the reasoning for Y." `/prepare-reset` appends compacted narrative
> here as it trims `MASTER_PLAN.md`.
>
> **Split established 2026-07-08.** Pre-split narrative (through ~2026-07-04) lives in
> [`completed/2026-07-04-master-plan-archive.md`](completed/2026-07-04-master-plan-archive.md); the
> dense 2026-07-05→08 header narrative still in `MASTER_PLAN.md` is the **first migration target** — a
> deliberate fresh-session task (see `/prepare-reset` Step 3 judgment guard: don't deep-restructure the
> live plan at heavy context).

---

## 2026-07-08 — dispatch tooling + review-gate + ticketing process

- **FRE-847** `context_probe` shipped — headless per-session context% + idle from the transcript JSONL
  (`input+cache_read+cache_creation`, matches `/context`). Rejected the statusLine approach (would edit a
  CC-managed file); `idle_s`/`state` is a weak proxy, the pane (`session_is_idle`) is authoritative.
- **FRE-848** watcher context-pressure alert shipped + deployed — gating-watcher nudges cc-master to
  checkpoint at ≥70% idle; watcher restarted onto the new code. Dogfooded the new review gate (build
  found + folded 2 real bugs, improved on the spec).
- **Review gate → shift-left:** build self-reviews (`/code-review` + `/security-review`, effort-sized) once
  pre-PR, hands master a self-review summary; master validates as executive + decides, keeps veto over
  fold-ins. Encoded in build/master skills.
- **Anti-over-ticketing:** a ticket is an objective (user story), not a boxed single change; build folds
  non-ADR supporting changes + review findings into the PR — no paper-trail tickets. Encoded in build/master.
- **New skills:** `/prepare-reset` (safe wind-down + decision-distillation + MASTER_PLAN compaction, bookend
  to `/prime-master`). MASTER_PLAN split into concise-plan + this history file.
