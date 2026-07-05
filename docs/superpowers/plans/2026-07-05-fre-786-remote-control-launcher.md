# FRE-786 Part 2 — Remote Control launch primitive (`scripts/dispatch/launcher.py`)

**Ticket:** FRE-786 (Approved, In Progress, Tier-1:Opus, stream:build1)
**Backing ADR:** ADR-0110 — External Dispatch Orchestrator (§2 launcher, §4 graceful degradation)
**Carries AC:** AC-2 (context contract + model tier), AC-3 (owner answers remotely — *proven live in Part 1*),
AC-7 part a (bounded model-set fallback).
**Predecessor:** FRE-785 shipped `scripts/dispatch/next_resolver.py` (pure/IO split, stdlib-only). This
mirrors that style.

## Context established

- **Part 1 (RC mechanics spike) is complete, owner-present, 2026-07-05.** All three mechanics proven live:
  (a) auto-seed a slash command as the first turn ✅, (b) programmatic model-set at launch ✅,
  (c) completion signal via `claude agents --json` per-session status ✅. AC-3 proven (owner answered off-box).
- **Launcher gotchas from the spike (must bake in):** (1) **trust gate** — launch only into an
  already-trusted worktree, never a fresh dir; (2) **keep stdin on the PTY** — launch bare inside tmux, never
  through a pipe (a piped launch goes non-interactive and exits after one turn); (3) **tmux is the launch
  substrate** — `tmux new-session -d -s <cc-stream> -c <worktree> 'claude --remote-control … --model …
  --session-id <uuid> "<seed>"'` gives RC its PTY *and* a local attach seat; (4) **deterministic session-id**
  so the launched session is addressable in `claude agents --json`.
- **Scope additions agreed with owner (Part 2):** named tmux session per worker (two monitoring seats);
  pre-launch worktree hygiene for CLEAR (reset to a clean base off `origin/main`).

## Design — mirror `next_resolver.py`'s pure/IO split

`scripts/dispatch/launcher.py`, stdlib-only, `from __future__ import annotations`, frozen dataclasses,
Google docstrings, argparse CLI with `main(argv) -> int`. **Dry-run by default; `--execute` performs the
side effects.** No `src/` change — dev-process tooling under `scripts/`, matching the resolver's script style
(plain `print` for CLI output; the resolver already does this and passes ruff/pre-commit).

### Stream topology (pure constant)

```
build1 → worktree .claude/worktrees/build  · tmux cc-build   · command "/build 1"
build2 → worktree .claude/worktrees/build2 · tmux cc-build2  · command "/build 2"
adr    → worktree .claude/worktrees/adrs   · tmux cc-adrs    · command "/adr"
```

Encoded as a frozen `StreamTopology` dataclass + a `_TOPOLOGY: dict[str, StreamTopology]` map.

### Capabilities (forced-off for AC-7a)

`LauncherCapabilities(auto_seed: bool, model_set: bool)` — **defaults both `True`** (established live in
Part 1, not re-probed; honest — we don't pretend to probe what the owner already proved by hand). CLI flags
`--no-auto-seed` / `--no-model-set` force them off to exercise the fallbacks (AC-7a's "forced off" check).

### `LaunchPlan` — discriminated union (the proof surface)

```
outcome: Literal["launch", "prepare", "manual-model-required", "manual-continuation"]
stream, model, context ("clear"|"keep"), tmux_session, worktree: str
session_id: str | None          # deterministic uuid5(_NS, f"{stream}:{ticket}:{model}:{context}") — includes the
                                 # ticket id so two tickets on the same stream/model/context get distinct ids
                                 # (codex #1); addressable + never resumes a prior ticket's context. Set for
                                 # launch/prepare; for manual-continuation carries the resolved warm session id.
command: tuple[str, ...] | None  # the tmux argv to run; None for manual outcomes
reset_worktree: bool             # True only for a CLEAR launch/prepare
card: str                        # device-visible message; for manual outcomes this IS the deliverable
```

`plan_launch(stream, ticket, model, context_keep, capabilities, warm_session_id=None) -> LaunchPlan` (pure).
**`model` is validated against the tier set `{opus, sonnet, haiku}` and `stream` against the topology keys —
any other value raises `ValueError`** (codex #4: neither reaches a shell as free-form input):

| Inputs | outcome | why (ADR §4 / AC) |
|---|---|---|
| CLEAR, model_set=T, auto_seed=T | `launch` | full machine launch: `--model`, `--session-id`, seeded `/build N`; reset_worktree=T |
| CLEAR, model_set=T, auto_seed=F | `prepare` | modeled fresh session, no seed; card surfaces exact command to tap-send; reset_worktree=T |
| CLEAR, model_set=F | `manual-model-required` | **no launch** — card names exact model + command (AC-7a: never launch at an unproven model) |
| KEEP (any caps) | `manual-continuation` | **never machine-launch, never reset/clear.** Card **names the required model tier and states the launcher has NOT proven/switched it** (codex #2 — a `cwd` match does not prove the warm session's active model), and continues in the warm session id **only on an exact single-cwd-match**; zero or multiple matches → "unproven warm target" manual card (AC-2 KEEP) |

The command (launch/prepare) is built by `_build_tmux_command(...)`:
`("tmux", "new-session", "-d", "-s", <cc-stream>, "-c", <worktree>, <claude-invocation>)`. The final arg is
the claude invocation, which **tmux parses as a shell command** — so it is assembled with **`shlex.join`**
over a validated argv (`claude`, `--remote-control`, `<cc-stream>`, `--model`, `<validated-model>`,
`--session-id`, `<uuid>`, and for `launch` only the seed `/build N`), never string-concatenated (codex #4).
**Never piped** (gotcha 2).

### Execution seam (IO, injectable runner)

`execute_plan(plan, runner=subprocess_runner) -> LaunchResult`:
- manual outcomes → no side effect; return the card.
- launch/prepare → if `reset_worktree`, run **guarded** hygiene first (see below); then run the tmux command
  via `runner`. `runner` is a `Callable[[Sequence[str]], CompletedProcessLike]` seam so tests inject a fake.

**Guarded worktree hygiene** (`_preflight_worktree`, CLEAR only) — resolves the "reset to clean base off
origin/main" scope-addition *without* violating "unstaged changes are mine":
1. `git -C <worktree> fetch --prune origin`
2. Check `git -C <worktree> status --porcelain` — **if non-empty, ABORT the launch** and return a
   `worktree-dirty` refusal card (never destroy uncommitted work).
3. If clean, the worker's own `/build` Step 0 cuts the fresh `fre-XXX` branch off `origin/main`. **The
   launcher does not itself `git reset --hard` or `switch` — it only fetches + verifies-clean, delegating the
   branch cut to the skill.** This **preflights the dirty-worktree case only**; `/build` Step 0 owns the full
   safety gate (clean status *and* no unpushed current-branch work — codex #3). The launcher does not
   duplicate the unpushed-work check.

**Warm-session probe** (`find_warm_session(stream, runner)`, IO): parse `claude agents --json --all`; return
the `sessionId` **only on an exact single match** whose `cwd` == the stream's worktree; **`None` on zero or
multiple matches** (codex #2 — never guess which of several sessions is the warm one). Enriches the KEEP card.

**tmux failure handling** (codex #4): `execute_plan` treats a non-zero `tmux new-session` (including an
already-existing named session) as a `launch-failed` refusal card — never a claimed launch.

### CLI

```
python -m scripts.dispatch.launcher --stream build1 --model opus            # dry-run: prints the plan + card
python -m scripts.dispatch.launcher --stream build1 --model opus --keep     # KEEP → manual-continuation card
python -m scripts.dispatch.launcher --stream build1 --model opus --no-model-set   # → manual-model-required
python -m scripts.dispatch.launcher --stream build1 --model opus --execute  # actually launch (owner/seam use)
python -m scripts.dispatch.launcher --stream build1 --model opus --json     # plan as JSON
```

Dry-run prints the discriminated outcome + the exact command/card — this is what makes AC-2 and AC-7a
inspectable without a live RC session. **`--execute` is the owner/seam path** and is not exercised by unit
tests (no live `claude`/`tmux` in CI).

## Steps

1. **Write failing tests** `tests/scripts/test_launcher.py` (mirror `test_next_resolver.py` style, `ruff:
   noqa: D103`). Cover:
   - `plan_launch` CLEAR full-caps → `launch`; command argv contains `--model opus`, `--session-id`, the
     seed `/build 1`, targets `.claude/worktrees/build`, tmux `cc-build`, `reset_worktree` True. **(AC-2 CLEAR
     intent)**
   - `plan_launch` KEEP → `manual-continuation`; `command is None`, `reset_worktree` False; with/without a
     `warm_session_id` the card differs but never becomes a launch. **(AC-2 KEEP)**
   - `plan_launch` CLEAR `model_set=False` → `manual-model-required`; `command is None`; card contains the
     exact model (`opus`) and the exact command (`/build 1`); no launch. **(AC-7a)**
   - `plan_launch` CLEAR `auto_seed=False, model_set=True` → `prepare`; command present (no seed positional),
     card surfaces `/build 1` to tap-send. **(ADR §4 middle degradation)**
   - `plan_launch` KEEP card **names the required model tier** and states it is not proven/switched. **(codex #2)**
   - deterministic `session_id` — same (stream,ticket,model,context) → same uuid; **different ticket on the
     same stream/model/context → different uuid**; different stream/model → different uuid. **(codex #1)**
   - `_build_tmux_command` never contains a shell pipe; is `-d` detached; keeps the claude invocation as one
     `shlex.join`ed arg (PTY intact). **(gotcha 2/3)**
   - **shell-metacharacter safety** — a `model`/`stream` outside the validated sets raises `ValueError`; the
     built command never contains an unescaped metacharacter from inputs. **(codex #4)**
   - `execute_plan` with an injected fake runner: launch outcome → runner called with the tmux argv; manual
     outcomes → runner **never** called (no side effect). KEEP → runner never called (never resets). **(AC-2
     KEEP: no fresh/clear)**
   - `execute_plan` CLEAR with a fake runner reporting a **dirty** `git status` → aborts, `worktree-dirty`
     card, tmux **never** invoked. (hygiene guard / "unstaged changes are mine")
   - `execute_plan` launch where the fake runner returns non-zero for `tmux new-session` (session exists) →
     `launch-failed` card, not a claimed launch. **(codex #4)**
   - `find_warm_session`: single cwd match → its `sessionId`; **zero matches → `None`; multiple matches →
     `None`.** **(codex #2)**
   - unknown stream / unknown model → `ValueError`.
   - `main(["--stream","build1","--model","opus"])` dry-run → exit 0, prints outcome `launch`;
     `--no-model-set` → prints `manual-model-required`; `--keep` → `manual-continuation`.
   Run: `make test-file FILE=tests/scripts/test_launcher.py` → expect **fail** (no module yet).

2. **Implement** `scripts/dispatch/launcher.py` per the design. Re-run the file test → **all pass**.

3. **Docstring / doc touch**: module docstring documents the four outcomes + the three preconditions from the
   ticket (RC entitlement, claude.ai login on VPS, RC disabled off-Anthropic-endpoint). No README changes
   needed (orchestrator/runbook are FRE-787/788).

4. **Quality gates**: `make test-file FILE=tests/scripts/test_launcher.py` → `make test` (full) → `make mypy`
   → `make ruff-check` + `make ruff-format` → `pre-commit run --all-files`.

5. **PR** off `origin/main`, ticket handoff comment to master with the AC-proof table + the seam division
   (what's proven-in-PR vs deferred-to-T3-live-dispatch) + preconditions runbook.

## Acceptance-criteria proof (what this PR proves vs. what the seam proves)

| AC | Proven in this PR (unit/dry-run) | Deferred to T3/seam (live, owner-present) |
|---|---|---|
| AC-2 | planner emits fresh-session `launch` at labeled model for CLEAR; `manual-continuation` (never fresh) for KEEP; dry-run shows the **intended seed argument** `/build N` in the command (codex #5 — the argv, not a proof RC ran it) | one live CLEAR + one live KEEP dispatch: session self-reports tier, no prior context, first action invokes the skill |
| AC-3 | — (already proven live in Part 1) | already satisfied |
| AC-7a | `model_set=False` → `manual-model-required` naming exact model+command, no launch; never launches at an unproven model | force mechanics off on a real dispatch |

The ADR's Testing strategy already assigns live RC validation to T3/seam ("Remote Control inherently requires
the owner's device"); this PR delivers the **primitive + its bounded fallbacks, fully unit-proven**.

## Codex plan-review outcome (2026-07-05): **approve with changes** — all 6 folded in above

1. session_id now includes the ticket id (distinct per ticket). 2. KEEP card names the required model + states
it is unproven; warm-session lookup is single-match-only. 3. hygiene claim softened to "preflights dirty case
only"; `/build` Step 0 owns the full gate. 4. model/stream validated against fixed sets; inner command via
`shlex.join`; tmux-failure → refusal card; metachar tests added. 5. AC-2 proof reworded to "intended seed
argument" (argv, not a live-run proof). 6. `post_merge_prune` dropped from Part 2 — deferred to FRE-787.

## Open questions for the OWNER (approval gate)

1. **Post-merge prune split** — the launcher stays the *launch primitive*; post-merge branch/worktree prune is
   an advance-step and moves to the orchestrator loop (FRE-787). Confirm this split (vs. folding a prune helper
   into this PR).
2. **Owner-present / seam division** — Part 2 = build `launcher.py` + unit tests + dry-run (deterministic, no
   live owner needed). The **live** AC-2 (fresh session self-reports tier, first action invokes the skill) and
   AC-3 are already/again the **T3 seam** master owns, per ADR Testing strategy ("RC inherently requires the
   owner's device"). Confirm it's fine to ship Part 2 now and prove the live dispatch at the seam — rather than
   holding this ticket open for a live owner-in-loop session.
