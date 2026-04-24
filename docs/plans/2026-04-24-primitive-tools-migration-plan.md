# Primitive Tools Migration Plan

**Date**: 2026-04-24
**Status**: Approved (2026-04-24)
**ADR**: [ADR-0063](../architecture_decisions/ADR-0063-primitive-tools-action-boundary-governance.md)
**Master Plan entry**: Wave 2.5 (parallel to Wave 2 feedback streams)
**Linear Epic**: [FRE-259](https://linear.app/frenchforest/issue/FRE-259)

---

## Purpose

Sequence the transition described in ADR-0063 so that:

1. Wave 2 (FRE-246 / 244 / 247 / 248 — feedback streams) keeps shipping on `main` without disruption.
2. Every phase is independently shippable and independently reversible.
3. Every phase produces evidence that informs the next phase's go/no-go decision.
4. Research investment in pre-LLM Gateway stages is preserved.
5. Security properties encoded today as pre-flight filters are re-applied at action-boundary without capability loss.

---

## Branching strategy

**Target**: `main`.

Work is not long-lived-branched. Each phase is a short-lived feature branch off `main`, PR'd, merged. Main keeps shipping Wave 2 in parallel.

Rationale: the earlier draft proposed a single long-lived `feat/primitive-tools-pivot` branch. Each phase here is self-contained and additive until P4 — long-lived branching would accumulate merge conflicts with Wave 2 code unnecessarily.

---

## Wave 2 coexistence

Wave 2 (feedback streams: FRE-246 Mode Manager, FRE-244 Error Patterns, FRE-247 Insights, FRE-248 Self-Improvement) ships on `main` concurrently. No intersections in code. Two indirect interactions:

- **FRE-246 Mode Manager fix** is a *prerequisite* for pivot phase 2 — the mode state must be correct before action-boundary governance reads it.
- **FRE-244 Error Patterns** will gain a new pattern post-pivot ("sandbox execution failure") but the stream schema and consumer wiring are unaffected.
- **FRE-226 skills (originally Wave 4)** moves inside the pivot at phase 3. Wave 4 position is re-scoped to "agent writing its own skills (phase 2 of FRE-226)."

FRE-249, FRE-250, FRE-251 — no interaction.

---

## Security property map

Every safety property in today's pre-flight governance has a destination in the new model.

| Current property | Current location | New location |
|---|---|---|
| `allowed_in_modes` per tool | `tools.yaml` + `evaluate_governance()` | Retained unchanged for retained API wrappers. For primitives: per-primitive mode-gate in `ToolExecutionLayer.execute()` action-boundary check. |
| `forbidden_paths` / `allowed_paths` | `tools.yaml` (read_file / write_file) | `read` / `write` primitives inherit the same fields; enforcement moved into the primitive executor. |
| `requires_approval` / `requires_approval_in_modes` | `tools.yaml` + MCP gateway | `bash` command allowlist + AG-UI `tool_approval_request` for non-auto commands. Per-mode allowlist defined in `tools.yaml` (new `bash.auto_approve_prefixes` / `write.approval_required_outside` sections). |
| `rate_limit_per_hour` | `tools.yaml` + tool executor | Retained for API wrappers. For `bash`: rate limits keyed by command prefix (logical tool name). |
| `requires_outbound_gateway` (network) | `tools.yaml` + specific tool | `run_python`: network-off by default; `network: true` arg requires approval. `bash`: network-capable commands (`curl`, `wget`, `gh`) all pre-checked through the allowlist. |
| `loop_max_per_signature` / `loop_max_consecutive` / `loop_output_sensitive` | `tools.yaml` + ADR-0062 loop gate | Retained for all tools (primitives get entries too). Signal severity split per ADR-0063 D5. |
| `forbidden_in_modes` | `tools.yaml` | Retained; LOCKDOWN disables `bash` / `write` / `run_python`, keeps `read` restricted to essential paths. |
| `max_file_size_mb` | `tools.yaml` (read_file) | Inherited by `read` primitive. |
| Subprocess allowlist (`run_sysdiag`) | Tool-specific config | Folded into `bash` first-word allowlist; same set of binaries, plus curated additions. |

**No safety property is lost. All are re-expressed in the new mechanism.**

---

## Phases

### Phase 1 — Sever TaskType → tool-filter wire

**Scope:**
- Delete `task_type_policies` block from `config/governance/tools.yaml`.
- Remove `allowed_tool_categories` computation in `request_gateway/governance.py:evaluate_governance()`.
- Remove `GovernanceContext.allowed_tool_categories` field or mark deprecated.
- Remove consumer in `orchestrator/executor.py` (the `_allowed_cats` block at lines ~1353-1371 and the `is_synthesizing = True` branch on empty `tool_defs`).
- `get_tool_definitions_for_llm()` keeps the `allowed_categories` parameter signature but its only caller (executor) passes `None`. Reconsider parameter in phase 6.

**What stays:**
- Intent classifier runs as before and produces `task_type`.
- `task_type` continues to drive iteration budgets, decomposition, memory retrieval, context depth, telemetry.
- Mode enforcement (`allowed_in_modes` per tool) unchanged.
- Anthropic `cache_control` on tool list (FRE-254 caching) keeps the static tool prefix cached.

**Not in scope:**
- No new primitives yet.
- No tool deprecation.
- No governance rewrites.

**Tests:**
- Existing tests that assert `allowed_tool_categories` contents must be updated to reflect the removed field or deleted.
- New test: `conversational` intent + production prompt → tools are passed with `count > 0` (the exact regression the pivot addresses).

**Risk:** Low.

**Gate to P2:**
- 48h of production traces show prompt-cache hit rate ≥ 70% (Anthropic cache read).
- Mean tokens-per-prompt within 5% of pre-P1 baseline (measured via `litellm_request_complete.prompt_tokens`).
- Zero occurrences of the FRE-254 failure class (empty `tool_defs` on a non-DISABLED strategy).

**Reversibility:** Single revert commit. Config file change + ~30 LOC executor change.

**Linear issue:** PIVOT-1.

---

### Phase 2 — Four primitive tools + sandbox + action-boundary governance

**Scope:**
- New tools: `bash`, `read`, `write`, `run_python`.
- Sandbox design and implementation.
- AG-UI `tool_approval_request` message type.
- PWA approval UI component.
- `tools.yaml` entries for the four primitives with per-mode allowlists.

**Sandbox design:**

`run_python`:
- Short-lived Docker container, image: `python:3.12-slim` + a curated set of libraries (requests, httpx, pandas, numpy, json, pyyaml).
- Runtime: non-root UID (`appuser:1000`), read-only root filesystem.
- Filesystem: bind-mount `/app/sandbox/<trace_id>/` (host) → `/sandbox` (container). Scratch only; persisted in captures for forensic replay.
- Network: disabled by default. Opt-in via `run_python(script, network=True)` — triggers approval.
- Timeout: default 60s, max 300s; hard-killed on overrun.
- Seccomp: default Docker profile (restrictive).
- Resource limits: `--memory=512m --cpus=1.0` to prevent resource exhaustion.
- Lifecycle: container created per invocation, removed post-execution. Output (stdout + stderr + exit code) returned as tool result.

`bash`:
- Runs in the agent service container (needs access to already-installed `curl`, `docker`, `gh`, `psql`, `redis-cli`, etc.).
- Pre-execution parse: extract first word, match against `tools.yaml` `bash.allowlist` per current mode.
- Hard-deny patterns (regex): `\brm\s+-rf\b`, `\bdd\s+if=`, `\bmkfs\b`, `\bsudo\b`, `\bwget\b` (prefer `curl`), `\bssh\b`, `\bnc\s+-l\b`, `:(){ :|:& };:` (fork bomb).
- Soft-deny (approval required): any first-word not in auto-approve, any command containing `>` or `>>` redirect to outside scratch, any `docker exec` targeting another container.
- Timeout: default 30s, max 120s.
- Output: stdout + stderr + exit code (capped at 50KB, larger output written to `/app/sandbox/<trace_id>/bash_output_<n>.txt` and path returned).

`read`:
- Inherits `allowed_paths` / `forbidden_paths` from current `read_file` config.
- Max bytes: inherits `max_file_size_mb`.
- Governance: path check only; no approval in NORMAL/ALERT/DEGRADED; LOCKDOWN allows only `/app/**` essential paths.

`write`:
- Inherits `allowed_paths` / `forbidden_paths` from current `write_file` config.
- Unattended scope: `/app/sandbox/**`, `/tmp/**` — no approval.
- Outside unattended scope: AG-UI approval prompt.
- Mode: LOCKDOWN disables entirely; DEGRADED/ALERT scratch-only.

**Action-boundary approval protocol:**

New AG-UI event type: `tool_approval_request`:
```json
{
  "type": "tool_approval_request",
  "request_id": "uuid",
  "trace_id": "uuid",
  "tool": "bash",
  "args": {"command": "docker exec cloud-sim-postgres psql -c 'SELECT 1'"},
  "risk_level": "medium",
  "reason": "command not in auto-approve allowlist",
  "expires_at": "2026-04-24T05:30:00Z"
}
```

PWA receives → renders modal → user clicks approve/deny → PWA posts `tool_approval_response` to `/agui/approval/{request_id}`.

Agent service blocks on approval with configurable timeout (default 60s, timeout = deny). All decisions (approve/deny/timeout) captured to `agent-captains-captures-*.approval_decisions[]`.

**Auto-approve allowlist (initial, NORMAL mode):**

`bash`:
- `curl`, `grep`, `ls`, `cat`, `find`, `jq`, `docker ps`, `docker logs`, `git log`, `git status`, `git diff`, `psql -c 'SELECT`, `redis-cli GET`, `ps`, `top -bn1`, `free`, `df`, `uptime`, `wc`.

Tuned from telemetry after 2 weeks.

**Skill docs (P2 subset):**

Ship skeletal skill docs for the four primitives themselves (not the deprecated tools — that's P3):
- `docs/skills/bash.md` — command patterns, examples, caveats.
- `docs/skills/run-python.md` — sandbox constraints, usable libraries, network approval.
- `docs/skills/read-write.md` — path scopes per mode.

**Tests:**
- Unit tests per primitive (mocked sandbox).
- Integration test with Docker sandbox for `run_python`.
- Pentest script attempting: sandbox escape, network exfiltration, privilege escalation, path traversal, fork bomb, resource exhaustion.
- Approval flow test: primitive fires approval, PWA receives, approves, execution resumes.

**Risk:** Medium. New attack surface (sandbox), new UX (approval prompts).

**Gate to P3:**
- Pentest report: zero successful escapes or policy violations.
- Approval flow tested end-to-end via cloud-sim docker-compose.
- Mean approval-to-decision time in user testing < 15s.
- No regression on existing tool success rate (no deprecations yet — all 15 tools still work).

**Reversibility:** Feature flags `AGENT_PRIMITIVE_TOOLS_ENABLED`, `AGENT_APPROVAL_UI_ENABLED`. Default off until gate passes. Rollback = flip flags.

**Linear issue:** PIVOT-2.

**Prerequisites:** FRE-246 (Mode Manager fix, ADR-0055) must be complete — action-boundary governance reads mode state.

---

### Phase 3 — Skill docs + model evaluation

**Scope:**
- One skill doc per deprecated tool (8 docs):
  - `docs/skills/query-elasticsearch.md` (replaces `self_telemetry_query` + `query_elasticsearch`)
  - `docs/skills/fetch-url.md` (replaces `fetch_url`)
  - `docs/skills/list-directory.md` (replaces `list_directory`)
  - `docs/skills/system-metrics.md` (replaces `system_metrics_snapshot`)
  - `docs/skills/system-diagnostics.md` (replaces `run_sysdiag`)
  - `docs/skills/infrastructure-health.md` (replaces `infra_health`)
  - (`read_file` → `read` doesn't need a new doc; primitive signature is self-documenting.)

- Skill-doc injection mechanism into system prompt (either always-on for all skills, or dynamic based on task_type — TBD in phase).

- Evaluation harness: 20 representative user prompts covering the deprecated tools' use cases (e.g. "check recent errors in logs", "read /app/config.yaml", "is postgres reachable", "what's the system CPU load").

- Dual-path eval: for each prompt, run with `AGENT_LEGACY_TOOLS_ENABLED=true` (curated tools visible) and with `AGENT_LEGACY_TOOLS_ENABLED=true` + `AGENT_PREFER_PRIMITIVES=true` (skill docs injected, model nudged to prefer primitives). Compare success rate, turn count, token usage.

**Gate to P4:**
- Primitive success rate ≥ curated success rate on the 20-trace eval.
- No eval failure that traces to "model could not figure out the primitive equivalent."
- If primitive success < curated, that specific tool stays (not deprecated in P4) until the skill doc improves.

**Reversibility:** Skill docs are additive — no removal. `AGENT_PREFER_PRIMITIVES` flag controls nudging.

**Linear issue:** PIVOT-3.

---

### Phase 4 — Flag-gated deprecation

**Scope:**
- `AGENT_LEGACY_TOOLS_ENABLED` env flag added, default `false`.
- When `false`: deprecated tools not registered in the tool registry — model doesn't see them.
- When `true`: old behavior preserved.
- Emit `tool_deprecated` warning log every time the flag is `true` in production (observability for tracking rollback status).
- Update `docs/skills/*.md` status to "Active (legacy tool deprecated)".

**Gate to P5:**
- 2 weeks of production traces with `AGENT_LEGACY_TOOLS_ENABLED=false` show no regression in:
  - Task success rate (measured via Captain's Log `outcome=completed` rate)
  - User-reported friction (Linear bugs tagged `tool-regression`)
  - Agent-reported friction (error logs, fallback reply counts)

**Reversibility:** Flip flag back to `true`. Old code still present.

**Linear issue:** PIVOT-4.

---

### Phase 5 — Loop gate signal split + `model_config` fix

**Scope:**
- `ToolLoopGate.check_before()` splits signal severity per ADR-0063 D5:
  - `BLOCK_OUTPUT` — terminal (unchanged).
  - `BLOCK_IDENTITY` — advisory at `loop_max_per_signature`, terminal at `loop_max_per_signature + 2`.
  - `BLOCK_CONSECUTIVE` — advisory only; `WARN_CONSECUTIVE` becomes the only consecutive outcome.
- New `GateDecision.ADVISE_*` variants; executor appends advisory hints to tool result content without blocking.
- Telemetry fields unchanged except `decision` gains ADVISE variants.
- Fix `executor.py:1296` — resolve model_config through active ExecutionProfile when present.
- Tests updated for new advisory behavior.

**Gate to P6:**
- No uptick in `task_failed` events attributable to tool loops.
- Existing output-identity blocks still fire (FSM unchanged for that signal).
- Pentest: simulated malicious loop (model calling `bash rm -rf /tmp/*` 100 times) still terminated at the max-terminal ceiling.

**Reversibility:** Revert is one file + config; no schema changes.

**Linear issue:** PIVOT-5.

---

### Phase 6 — Delete legacy tool code

**Scope:**
- Remove: `read_file`, `list_directory`, `system_metrics_snapshot`, `self_telemetry_query`, `query_elasticsearch`, `fetch_url`, `run_sysdiag`, `infra_health` tool modules.
- Remove their entries from `src/personal_agent/tools/__init__.py` registration.
- Remove their entries from `config/governance/tools.yaml`.
- Remove tests.
- Remove `AGENT_LEGACY_TOOLS_ENABLED` flag and related branches.
- Remove `allowed_categories` parameter from `get_tool_definitions_for_llm()` if no remaining callers (phase 1 left it dead).

**Gate:** `AGENT_LEGACY_TOOLS_ENABLED=false` untouched in all environments for ≥2 weeks after P4.

**Reversibility:** At this point, git revert is the mechanism. Code is gone from HEAD.

**Linear issue:** PIVOT-6.

---

## Coexistence with Wave 2

```
                     main
                      │
              ┌───────┴───────┐
              │               │
          Wave 2          Wave 2.5 (this plan)
              │               │
   FRE-246 Mode Manager ──► (prerequisite for P2)
   FRE-244 Error Patterns ── parallel, no conflict
   FRE-247 Insights      ── parallel, no conflict
   FRE-248 Self-Improve  ── parallel, no conflict
                              │
                    ┌─────────┼─────────┐
                    ▼         ▼         ▼
                   P1        P2 (blocked on FRE-246)
                    │         │
                    ▼         ▼
                   P3        P4
                    │         │
                    └────►   P5
                              │
                              ▼
                             P6

FRE-226 skills (was Wave 4) → absorbed into P3 (hand-authored docs);
   FRE-226 phase 2 (agent self-updating skills) remains Wave 4.
```

**Merge discipline:**
- Pivot PRs rebase on `main` before merge.
- No Wave 2 PR touches `request_gateway/governance.py` or `orchestrator/executor.py:step_llm_call` during P1 window (48h freeze post-merge of P1 for baseline measurement).
- After P1 merge and baseline measurement, no further freezes needed.

---

## Rollback scenarios

### P1 causes token-cost regression > 5%

Revert P1 commit. Re-measure. Investigate cache miss pattern — likely cause is a dynamic piece of the prompt invalidating cache prefix. Fix root cause, re-attempt P1.

### P2 pentest finds sandbox escape

Do not merge P2. Re-architect sandbox (stricter seccomp, separate host, gVisor). Keep primitive code behind feature flag until pentest clean.

### P3 eval shows primitive success rate below curated

Partial rollback: deprecate only the tools whose primitives meet-or-exceed. Keep the rest. Iterate on skill docs for the underperformers. Re-run eval.

### P4 produces tool-regression bugs

Flip `AGENT_LEGACY_TOOLS_ENABLED=true`. Diagnose. Fix skill doc or adjust allowlist. Try again.

### P5 causes loop-related task failures

Revert advisory change for the offending signal. Keep that signal terminal. Document per-tool override.

### P6 — no rollback; at this point the deprecation is final.

---

## Success metrics (cumulative)

| Metric | Measure | Target |
|---|---|---|
| FRE-254 class recurrence | Grep captures for `<tool_code>` pattern | 0 in 60d post-P1 |
| Token cost | Mean `prompt_tokens` + cache hit rate | ≤ 5% regression, cache hit ≥ 70% |
| Primitive adoption | % of tool calls that are primitives vs wrappers | ≥ 60% by P4+30d |
| New-capability iteration cost | Files touched to add a new agent capability | ≤ 1 (skill doc) post-P6 |
| Agent self-introspection capability | Agent can find a named function in its own source via `bash grep` | Achievable by P2 |
| Mean time to approve | Timestamp delta `tool_approval_request` → `tool_approval_response` | < 15s in user testing |
| Sandbox escape attempts | Pentest + production monitoring | 0 |

---

## Open items at plan approval

- [x] Linear epic created and linked. → [FRE-259](https://linear.app/frenchforest/issue/FRE-259)
- [x] 7 Linear issues created (PIVOT-1 through PIVOT-6, plus Wave 2 re-sequencing), all in state `Approved`: FRE-260 (P1), FRE-261 (P2), FRE-262 (P3), FRE-263 (P4), FRE-264 (P5), FRE-265 (P6), FRE-266 (PIVOT-WAVE2).
- [x] MASTER_PLAN.md updated with Wave 2.5 row and dependency graph amendment.
- [x] Wave sequence spec updated.
- [x] FRE-226 re-scoped (phase 1 hand-authored skills absorbed into PIVOT-3/FRE-262; phase 2 self-updating retains original Wave 4 position).
- [ ] Pentest checklist drafted for P2.
- [ ] Evaluation trace prompt list drafted for P3.
- [ ] Baseline metrics captured pre-P1 (prompt tokens, cache hit rate, tool success rate, iteration counts per task_type).
