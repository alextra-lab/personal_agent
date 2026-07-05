# FRE-788 — Dispatch orchestrator ops: service, guardrails, runbook (ADR-0110 T4)

**Ticket:** FRE-788 (Approved, Tier-2:Sonnet, stream:build1) · **Backing:** ADR-0110 §1, §4, §5, Risks table
**Branch:** `fre-788-dispatch-orchestrator-ops` · **Depends on:** FRE-787 (T3 loop, merged) · FRE-786 (T2 launcher, merged)

## Scope (from ticket body + ADR Risks table)

Operationalize the dispatch orchestrator. This is **dev-process tooling under `scripts/` + `infrastructure/` + `docs/`** — no `src/` behavior change (ADR Implementation Notes). Deliverables:

1. A **service unit for the Remote Control server** that restarts on failure (`Restart=always`).
2. A **liveness check** so the orchestrator **refuses to dispatch when RC is down**.
3. A documented **enable-once precondition** (entitlement + `claude auth login` + endpoint) that **fails fast when unmet**.
4. A **stall timeout** (exists from T3) paired with a **push-notification on a pending prompt** (native RC feature — documented, not built).
5. A **kill switch**.
6. The **runbook**: how the owner enables RC; how a stalled/failed run is surfaced + recovered; explicit **master-unchanged** statement.
7. Update process docs so the orchestrator is described **alongside** `prime-worker`, not silently replacing it.

## Acceptance criteria carried (the definition of done — from ADR Risks table)

- **AC-a** — The RC service restarts on failure (`Restart=always` in the unit) **and** the orchestrator refuses to dispatch against a down server. *Proof:* unit file contains `Restart=always`; a unit test kills the server (liveness stub → down) and asserts **zero launches** + a `dispatch_blocked` notify.
- **AC-b** — The enable-once precondition is documented **and fails fast when unmet**. *Proof:* `check_preconditions` returns not-ok for an off-Anthropic `ANTHROPIC_BASE_URL` / missing key; `main()` exits non-zero before the loop; unit tests assert both.
- **AC-c** — The runbook exists and states the master-unchanged invariant in plain terms. *Proof:* a test asserts the runbook file exists and contains the invariant sentence + the enable-once steps.

## Design decisions & the one open seam

- **Guardrails are RC-topology-independent.** Liveness probes RC *reachability* (`claude agents --json --all` exit 0), the precondition checks *static config*, the kill switch is a file flag — all correct whether the live substrate ends up being the ADR §1 **server-mode daemon** or the shipped T2 **per-session tmux launcher**.
- **Open seam flagged to master:** ADR §1 calls for a server-mode RC daemon on the *existing* worktrees ("not `--spawn worktree` clones"), while the shipped T2 launcher creates per-session RC in tmux. Server mode `--spawn same-dir` shares one cwd, so the per-worktree reading is `--spawn session` per stream (templated unit). T4 ships the server-mode unit as the ADR names it; **which substrate the live seam uses is master's T3/seam call (ADR §345)**. Recorded in the runbook + ticket comment.
- **Push on a pending prompt is native RC** (`/config` → "Push when actions required"; RC docs). We document enabling it. The orchestrator's stall notifier (T3 seam) stays a structlog warning surfaced in journald; no new push integration built (simplicity — grep found no existing push infra).

## Implementation steps (TDD — failing test first each step)

### Step 1 — Preconditions (fail-fast) · AC-b
`scripts/dispatch/orchestrator.py`. **Scope note (codex):** the static precondition covers only what is deterministically checkable from config — the **Linear API key** (resolver) and the **RC endpoint** (`ANTHROPIC_BASE_URL`). RC **auth/entitlement/subscription** cannot be proven from env; those are the human enable-once steps in the runbook, verified via `claude doctor` and, at runtime, by the liveness guard (RC unreachable → won't dispatch). Reasons are kept **distinct**, never conflated.
- `@dataclass(frozen=True) Precondition(ok: bool, reason: str)`.
- `is_anthropic_endpoint(base_url: str) -> bool` — empty/unset → True; else hostname must be `api.anthropic.com`.
- `check_preconditions(env: Mapping[str,str], api_key: str | None) -> Precondition` — not-ok reason `"linear-api-key-missing: …"` if no key; not-ok reason `"rc-endpoint-off-anthropic: …"` if `ANTHROPIC_BASE_URL` off-Anthropic; else ok. Docstring states auth/entitlement is *not* covered here (runbook + `claude doctor`).
- `main()`: replace the bare `if not api_key` with `check_preconditions(os.environ, api_key)`; on not-ok print `precondition unmet: …` and `return 1` **before** the loop.
- **Tests:** ok (key, no base_url); not-ok off-anthropic (distinct reason); not-ok missing key (distinct reason); `is_anthropic_endpoint` cases.

### Step 2 — Liveness + kill switch guard · AC-a
`orchestrator.py`. **Codex refinement:** liveness is **global RC reachability**, not per-stream substrate health — the docstring says so plainly, and the runbook repeats it (a passing probe means RC is reachable, not that a specific stream's session is up). The guard is a TOCTOU check (RC can die between the probe and `execute_plan`); that residual window is **backstopped by the stall timeout** — a launched-but-dead run produces no PR and takes the stall path. Acceptable and documented.
- `rc_server_alive(runner: CommandRunner) -> bool` — `runner(["claude","agents","--json","--all"]).returncode == 0`; docstring: global reachability only.
- `_kill_switch_engaged(path: Path) -> bool` — `path.exists()`.
- `_launch_block_reason(rc_alive: Callable[[],bool], kill_switch_engaged: Callable[[],bool]) -> str | None` — `"kill-switch"` if engaged, else `"rc-down"` if not alive, else `None`.
- `run_once(...)` gains `rc_alive: Callable[[],bool] | None = None` (default → `lambda: rc_server_alive(runner)`) and `kill_switch_engaged: Callable[[],bool] = lambda: False` (backward-compatible; existing `_run` helper unaffected — default runner returns rc 0 → alive, no kill file → not engaged).
- `_apply` **launch** case: after `if not execute: return`, compute `_launch_block_reason`; if set → `logger.warning("dispatch_blocked", …, reason=…)` + `notifier("dispatch_blocked", …, reason=…)` + `return` (no launch, no record — stream stays eligible).
- **Tests:** rc-down → zero `new-session`, one `dispatch_blocked` notify, no record (AC-a); kill-switch engaged → same; alive + no kill → launches (regression, existing tests still green); `rc_server_alive` rc0/rc1.

### Step 3 — `main()` wiring: kill-switch file + preflight
- `--kill-switch-file` (default `telemetry/dispatch.disabled`); build `kill_switch_engaged=lambda: _kill_switch_engaged(Path(args.kill_switch_file))`.
- `--preflight`: run `check_preconditions` + `rc_server_alive(subprocess_runner)`, print a one-line report, exit 0/1 (for the runbook + unit `ExecStartPre`).
- Pass `rc_alive`/`kill_switch_engaged` into the `tick()` `run_once` call.
- **Test:** `--preflight` returns non-zero when preconditions unmet (monkeypatched env).

### Step 4 — RC server-mode launch wrapper (new) · supports AC-a unit
`scripts/dispatch/rc_server.py` (imports `topology_for` from `launcher` — single source of truth, no path drift):
- `rc_server_plan(stream: str) -> tuple[str, tuple[str,...]]` — pure: returns `(worktree, ("claude","remote-control","--spawn","session","--name",f"seshat-{stream}"))`; `ValueError` on unknown stream.
- `main(argv)`: resolve plan; `--dry-run` prints it (so a human can verify the unit's ExecStart without launching); else `os.chdir(worktree)` (unit `WorkingDirectory=/opt/seshat`) + `os.execvp("claude", argv)`. **Codex cut:** no `--worktree-for` (unneeded for the ACs).
- **Tests (`tests/scripts/test_rc_server.py`):** correct worktree+argv for build1/build2/adr; `ValueError` unknown; `--dry-run` exit 0. (No over-assertion on dry-run text — the contract is topology + argv.)

### Step 5 — systemd units · AC-a (restart half)
`infrastructure/systemd/claude-remote-control@.service` (templated per stream):
```
[Unit]
Description=Claude Code Remote Control server for dispatch stream %i (ADR-0110 T4)
Documentation=https://linear.app/frenchforest/issue/FRE-788
After=network-online.target
Wants=network-online.target
[Service]
Type=simple
User=debian
Group=debian
WorkingDirectory=/opt/seshat
ExecStart=/opt/seshat/.venv/bin/python -m scripts.dispatch.rc_server %i
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal
[Install]
WantedBy=multi-user.target
```
`infrastructure/systemd/seshat-dispatch-orchestrator.service` (the loop daemon):
```
ExecStartPre=/opt/seshat/.venv/bin/python -m scripts.dispatch.orchestrator --preflight
ExecStart=/opt/seshat/.venv/bin/python -m scripts.dispatch.orchestrator --loop --execute
Restart=always
RestartSec=30
```
(full unit mirrors the soak-unit conventions; `WorkingDirectory=/opt/seshat`, journal output).
- **Test:** `claude-remote-control@.service` contains `Restart=always` and the `rc_server` ExecStart; orchestrator unit contains `--loop`, `Restart=always`, and a `--preflight` ExecStartPre.

### Step 6 — Runbook · AC-c
`docs/runbooks/dispatch-orchestrator.md`:
- **Enable-once precondition** (the fail-fast checklist): claude.ai subscription (not API key); `claude auth login`; workspace trust (run `claude` once per worktree); `ANTHROPIC_BASE_URL` unset/`api.anthropic.com`; `AGENT_LINEAR_API_KEY` set; `/config` → enable **Push when actions required**; `claude doctor` to diagnose entitlement.
- **Start/stop**: install units, `systemctl enable --now claude-remote-control@build …`; the orchestrator daemon; `--preflight` to verify.
- **Stall/failure surfacing + recovery**: `journalctl -fu seshat-dispatch-orchestrator` (stall = `dispatch_stall`/`dispatch_blocked`); recover by restarting the RC unit / clearing the kill switch.
- **Kill switch**: `touch telemetry/dispatch.disabled` halts all dispatch; remove to resume.
- **Master unchanged (verbatim invariant):** the orchestrator handles worker **dispatch only** — it never merges, deploys, closes tickets, or edits MASTER_PLAN; both approval gates (owner *whether*, master *when/where* + merge) are unchanged; workers still stop at "push branch + open PR".
- **Open seam** note (server-mode vs per-session tmux) for master's live verification.
- **Test:** runbook exists + contains the master-unchanged invariant sentence + "enable-once".

### Step 7 — Process-doc pointer
Add a short "Automated dispatch (ADR-0110)" note to `.claude/skills/prime-worker/SKILL.md` (top, near the monitor description): the orchestrator is an **ops layer that automates the switch→clear→type actuation the monitor only advises**; both read the same Linear-native dispatch contract; link the runbook. Update `infrastructure/systemd/README.md` Units table with the two new units.

## Quality gates (Step 8)
`make test` (module `tests/scripts/test_orchestrator.py tests/scripts/test_rc_server.py`, then full) · `make mypy` · `make ruff-check` + `ruff-format` · `pre-commit run --all-files`.

## Out of scope (explicitly)
- No `src/` change. No re-architecture of the T2 launcher (the server-vs-tmux seam is master's live call).
- No new push-notification integration (native RC push covers the pending-prompt case).
- No live systemd start / RC entitlement test (requires the owner's device + the VPS account — master's seam per ADR §345).
