# FRE-1238 — Untrack `config/governance/budget.yaml`

> **Ticket:** FRE-1238 (Approved, `stream:build2`, Tier-2:Sonnet, High)
> **Found by:** build2 while working FRE-1209; split out by master because it does not help build a dashboard.
> **Owner ruling 2026-08-09:** untrack + gitignore. **No history rewrite** — the caps stay in the nine
> historical commits and that is accepted. This ticket stops future drift; it does not remediate the
> past and must not claim to.

---

## Problem

`config/governance/budget.yaml` is tracked in a **public** repo and carries the live per-role spend
ceilings, with their change history in the comments. Nine commits, first landing `2a9241ff` (FRE-304).

Bounded severity, stated so nobody over-reacts later: these are **spend ceilings, not credentials and
not personal data**. Hygiene fix, not an incident.

## The constraint that shapes the design

The file is not inert config — three code paths read it, and one of them is a CI guard:

| Reader | Path | Runs where |
|---|---|---|
| `load_budget_config()` | `settings.governance_config_path / "budget.yaml"` | runtime (gateway) |
| `validate_role_totality()` | the runtime file, at startup | runtime (gateway) |
| `check_budget_role_coverage()` | `root / "config/governance/budget.yaml"` | **CI + pre-commit** |

Plus four test modules read the real file. So untracking alone **breaks a fresh clone and CI** — the
guard reports a safety finding and the cost-gate structural tests error.

**The invariant those readers actually enforce is the role *structure*** — that the role names in the
YAML agree with the `ModelRole` enum and `cost_gate/role_map.py`, and that every role is either capped
or explicitly listed in `uncapped_roles` (FRE-989: `study` sat capped in YAML but absent from the
resolver map, so it billed `main_inference` silently for months). **None of them read a dollar amount.**

That is the wedge: the *structure* is code-coupled and belongs in git; the *amounts* are operational
and do not.

## Design

1. **Untrack + ignore the real file.** `git rm --cached`, `.gitignore` entry. The file stays on disk —
   it leaves git, not the filesystem (AC-2).
2. **Commit `budget.yaml.example`** — the full role structure with **placeholder** amounts and a
   copy-to-use header. Operational history comments (observed peaks, right-sizing notes) stripped.
3. **Guard + tests fall back to the example when the real file is absent, and still validate it.**
   Not skip — *validate*. A skip is how a check quietly stops guarding (AC-3 names this explicitly).

**Production stays strict.** `load_budget_config()` is NOT given a fallback: a gateway whose real
`budget.yaml` has vanished must fail loudly rather than silently run on placeholder caps. Fail-open
defaults are a known failure family in this project; the fallback belongs only in the CI guard and the
tests, where "the file is absent" is the normal fresh-clone condition rather than a fault.

**Where the fallback lives:**
- `check_budget_role_coverage()` — falls back to `.example`, then validates it exactly as it would the
  real file. Absent *both* is still a safety finding.
- `tests/_helpers/budget_config.py` — `budget_config_path()` / `load_budget_config_for_tests()`, preferring
  the real file so a dev machine and the VPS keep validating the **deployed** caps (the stronger check),
  falling back to the template in CI.

## The deploy hazard (found by codex plan-review, not by the plan)

`infrastructure/scripts/deploy.sh` runs `git pull --ff-only` on the VPS before every rebuild, on all
three paths. **`git pull` deletes a file whose incoming commit removes it** — `.gitignore` protects
*untracked* files, it does not protect a tracked-file deletion arriving in a pull. So the very commit
that untracks `budget.yaml` would delete the real caps off the box, and cost-gate init is fatal at
startup: the gateway would not boot.

This is AC-2 failing in the worst possible way, and the original plan had no step for it.

**Fix:** snapshot the file before the pull and restore it after if the pull removed it, in all three
`REMOTE_CMD` paths. Restores only from a **non-empty** backup, so a box that genuinely has no
`budget.yaml` is left alone rather than handed an empty one that would fail validation confusingly.

Verified by simulation against a throwaway origin/clone pair reproducing the exact sequence
(tracked file → untrack commit → pull): naive pull **deletes** the file; guarded pull **preserves**
it; a box without the file **does not** get an empty one fabricated.

## Steps

| # | Step | Verify |
|---|---|---|
| 1 | `git rm --cached config/governance/budget.yaml`; `.gitignore` entry | `git ls-files --error-unmatch …` non-zero; `git check-ignore` matches (AC-1) |
| 2 | Write `config/governance/budget.yaml.example` | every `cap_usd` differs from the deployed value (AC-4) |
| 3 | `config_guard.check_budget_role_coverage` falls back to `.example` and validates it | new test: a tree with only a **perturbed** `.example` yields findings (AC-3's discriminating half) |
| 4 | `tests/_helpers/budget_config.py`; repoint the 3 structural test modules | `make test-k K=budget` green with the real file renamed away |
| 5 | Quality gates | `make test` · `make mypy` · `make ruff-check` · `pre-commit run --all-files` |
| 6 | AC-2 live probe | real file present on the box; `load_budget_config()` returns the real caps |

## Acceptance criteria → proof

| # | Criterion | Proof |
|---|---|---|
| AC-1 | Real file untracked and ignored | `git ls-files --error-unmatch` non-zero + `git check-ignore` output |
| AC-2 | Deployed file untouched, cost gate still reads it | file present on the box; `load_budget_config()` returns real caps (observed values recorded) |
| AC-3 | Fresh clone passes the guard, **validating** the example | guard exits clean on an example-only tree **and** returns findings on a perturbed example-only tree — both, or "validates" is unproven |
| AC-4 | Example carries no real cap | per-entry comparison of example vs deployed |

## Explicitly out of scope

- **History rewrite** — owner-rejected 2026-08-09. Recorded so the decision is visible, not absent.
- Moving caps into Postgres / building a sync path — considered and dropped during FRE-1209; the owner
  will audit config placement after Observability Foundation completes.
- Any other real-config-in-public-repo file. This ticket is `budget.yaml`.
