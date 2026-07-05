# FRE-806 — Refactor prime-worker for orchestrated dispatch

**Ticket:** FRE-806 (Approved→In Progress, Tier-1:Opus, stream:build1, **context:keep**) · **Backing:** ADR-0110 (launcher section + master-unchanged invariant)
**Branch:** `fre-806-prime-worker-orchestrated-dispatch` · **Related:** FRE-785/786/787/788 (T1–T4, all merged), FRE-781 (the bounce-follow loop this widens)

## Scope (from ticket body)

ADR-0110 moved dispatch resolution into the orchestrator (next-resolver decides, launcher starts). So prime-worker's own resolve/advise role is now **duplicated logic and a drift trap** — two independent resolvers of the same NEXT. Narrow prime-worker to the one thing only a live worker session can do: **monitor its own PR and self-fix.** Two couplings must land with it or it won't work under orchestration.

1. **Shed resolution + advise** — remove prime-worker Step 4 (resolve NEXT) and Step 5 (dispatch card); reframe as a pure PR-feedback monitor.
2. **Widen self-fix** — today the single self-fix trigger is a marked master bounce; add **CI-red** on its own open PR as a second trigger, same detect→ack→fix→`make test`→push→stop shape.
3. **Coupling (a): launcher seeds the ticket** — the launcher must carry the orchestrator-resolved ticket into the seed (`/build <FRE-id>` / `/adr <FRE-id>`) so the worker builds the intended ticket instead of re-deriving it.
4. **Coupling (b): the monitor loop is armed under orchestration** — today `/prime-worker` arms the 20m loop; under orchestration the seed is `/build <id>`, so the build/adr entry must arm the monitor itself (idempotently).

**Invariants unchanged:** master owns merge/deploy/close/dispatch; the worker never merges/deploys/clears; the 20m idempotent loop + ack-dedup are preserved.

## Acceptance criteria (definition of done — from the ticket)

- **AC1** — prime-worker no longer resolves NEXT or surfaces a dispatch card (Steps 4 & 5 gone). *Proof:* a structural test asserts the resolution/advise language is absent from the skill.
- **AC2** — the self-fix path triggers on **both** a marked master bounce **and** a red CI on its own open PR, same ack-fix-push-stop shape. *Proof:* a structural test asserts the CI-red FIX-MODE contract is present with an ack + dedup.
- **AC3** — the launcher seed carries the resolved ticket; a launched worker builds that exact ticket. *Proof:* `plan_launch(..., ticket="FRE-806")` produces a seed argv containing `/build FRE-806` (and `/adr FRE-806` for adr).
- **AC4** — the monitor loop is armed in the orchestrated launch path, not only by a manual `/prime-worker`. *Proof:* a structural test asserts the build (and adr) skill ends by arming the monitor idempotently.
- **AC5** — `make test`, `make mypy`, `make ruff-*` clean.

## Design notes

- **Auto-seed is CLEAR-only by construction (codex #2).** The launcher already refuses to machine-launch a `context:keep` ticket — `plan_launch` returns `manual-continuation` (no seed, no launch) for KEEP (ADR §2). So the `/build <id>` auto-seed path only ever carries a **CLEAR** ticket, and the build skill's "explicit `FRE-…` id → treat Context as CLEAR" is exactly correct for it. KEEP tickets stay a surfaced manual card. No conflict; documented so it is not ambiguous.
- **CI-red dedup — key by head SHA, not ack-time (codex #4).** The bounce dedups via a later `Ack: addressing master bounce` after the latest `## Master gate — BOUNCE`. CI-red has no comment marker, so its idempotency key is the **PR head SHA**: enter CI-red FIX MODE only when CI is **failing on the current head SHA** (not pending) **and** no PR comment `Ack: addressing red CI at <short-sha>` for *that* SHA exists. Ack (with the SHA) before fixing; the fix push changes the head SHA → CI goes pending on the new SHA → the "failing" gate is false until it re-runs. SHA-keying (not run-completion-time) avoids the stale-run / same-SHA-later-failure / late-completion ambiguities. The monitor loop is single-session and serial (one tick at a time), so concurrent-tick double-entry does not arise.
- **CI-red self-fix autonomy is ticket-authorized and bounded (codex #5).** Widening the worker to auto-fix red CI is exactly what this Approved ticket asks for; the ticket is the authorization. It stays bounded: fix on its own branch, `make test` green before push, **never merge or deploy**, master's gate unchanged. The Boundary section states the limit crisply.
- **These are process-doc + launcher changes; no `src/` change.** The launcher lives under `scripts/`. Structural skill tests (assert SKILL.md content) are the proof surface for AC1/AC2/AC4 — precedent: `tests/scripts/test_dispatch_runbook.py`.
- **Editing this session's own governing skills:** the armed loop `1d9cc7f5` reads the file fresh each tick, so after Step 2/3 land this session's monitor runs the new pure-monitor prime-worker. Expected; other worktrees stay on the old version until merge+sync (per-worktree skills).

## Implementation steps (TDD)

### Step 1 — Launcher seed carries the ticket (coupling a) · AC3 · `scripts/dispatch/launcher.py`
- Rename `StreamTopology.dispatch_command` → `skill_command`, values `/build`, `/build`, `/adr` (the base skill, no stream number).
- Add pure `seed_command(topology, ticket) -> str` returning `f"{topology.skill_command} {ticket}"`.
- In `plan_launch`: compute `dispatch = seed_command(topology, ticket)`; use it as the seed (auto-seed path) and pass it into the four card helpers (`_launch_card`/`_prepare_card`/`_manual_model_card`/`_manual_continuation_card`) in place of the old static `dispatch_command`.
- **Tests (`tests/scripts/test_launcher.py`):** update `test_topology_maps_each_stream` to `skill_command`; update the seed/card assertions from `"/build 1"` to `"/build FRE-786"` (ticket-carrying); add `test_seed_carries_resolved_ticket` asserting the seed argv contains `/build FRE-806` for a build stream and `/adr FRE-806` for adr. Confirm the failing form first.

### Step 2 — prime-worker sheds resolution + advise (coupling 1) · AC1 · `.claude/skills/prime-worker/SKILL.md`
- Delete **Step 4** (Resolve NEXT) and **Step 5** (dispatch card) entirely.
- Step 3.3 becomes: clean · nothing unpushed · no open PR → **idle → stay silent** (nothing to monitor; the orchestrator owns resolution).
- Update the **frontmatter description**, the intro ("monitor, not an executor"), the ADR-0110 note, and the **Boundary** to describe a **pure PR-feedback monitor**: it watches its own PR (bounce + CI-red self-fix) until merge and never resolves/advises/chooses work.
- Keep Step 1 (self-identify) + Step 2 (arm loop, idempotent) + Step 3 (state).

### Step 3 — Widen self-fix to CI-red (coupling 2) · AC2 · same file
- Restructure Step 3.2: **FIX MODE triggers on master-bounce OR CI-red** on its own open PR, same shape (detect → ack → fix on this branch → `make test` green → push → stop; never merge/deploy).
  - Bounce trigger + ack: unchanged (`## Master gate — BOUNCE` / `Ack: addressing master bounce`).
  - CI-red trigger + ack (**SHA-keyed**, codex #4): CI **failing on the current head SHA** (not pending) with no `Ack: addressing red CI at <short-sha>` for that SHA → ack (naming the SHA), fix, `make test`, push, stop. The push changes the head SHA, so re-runs key fresh and thrash cannot occur.
  - CI **pending** → one line, re-check next tick. CI **all green** → silent (master owns the gate).
- **Test:** structural assertions that the CI-red FIX-MODE contract (trigger, ack marker, fix-push-stop, never-merge) is present.

### Step 4 — build + adr skills arm the monitor EARLY (coupling b) · AC4 · `.claude/skills/build/SKILL.md`, `.claude/skills/adr/SKILL.md`
- **Arm early, not after the PR (codex #3).** Add an **arm-the-monitor step right after ticket verification / Step 0–1 reset, before the long TDD work** — check `CronList` for an existing `/prime-worker` loop; if none, arm `/loop 20m /prime-worker`. Arming early means a mid-build crash under an orchestrated `/build <id>` (whose entry was not `/prime-worker`) still leaves a monitor watching the branch/PR. During the build the monitor sees "Building" and stays silent, so early-arming is harmless to the manual flow. Idempotent `CronList` check → no double-arm when the manual `/prime-worker` already armed it.
- **Test:** structural assertions that both skills contain the idempotent monitor-arm step positioned before the build work.

### Step 5 — Quality gates · AC5
`make test` (module `tests/scripts/test_launcher.py tests/scripts/test_dispatch_skill_contracts.py`, then full) · `make mypy` · `make ruff-check` + `ruff-format` · `pre-commit run --all-files`.

## New/changed test files
- `tests/scripts/test_launcher.py` — updated (AC3).
- `tests/scripts/test_dispatch_skill_contracts.py` — new: AC1 (no resolve/card in prime-worker), AC2 (CI-red FIX MODE), AC4 (build+adr arm monitor).

## Out of scope
- No change to the dispatch **contract** (lifecycle-rules § Dispatch) — the orchestrator already implements it; prime-worker just stops duplicating it.
- No orchestrator.py change (it already threads `decision.ticket` into `plan_launch`).
- No live orchestrated-dispatch demo (master's assembled-seam verification, ADR §345).
