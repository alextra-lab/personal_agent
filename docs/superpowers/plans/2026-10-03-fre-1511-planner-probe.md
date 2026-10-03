# FRE-1511 — Commit the planner probe (ADR-0154 D7)

Backing ADR: `docs/architecture_decisions/ADR-0154-what-the-planner-sees-and-what-it-decides.md`
(D7, Appendix A1/A4, Implementation Notes "Eval-stack hazard"). Umbrella: FRE-1537.
Tier: Standard. The probe is the qualification gate of D7. A wrong threshold or a wrong
instrument admits a bad planner configuration. It touches no `src/` logic, schema, cost or memory.

## Scope

Port the archived 2026-10-03 probe (`adrs` worktree, git-ignored `telemetry/archive/fre1537-planner-probe/`)
to `scripts/eval/fre1537/`. Designs B and C are not ported. The archive's `a_prompts.json` step
becomes a repo step. The compose override becomes a standalone `docker run` launcher.

## Acceptance criteria (from the ticket) and their proof

| AC | Proof |
|----|-------|
| AC-1 runs from the repo | README lists one command per step. A test scans every module in `scripts/eval/fre1537/` and every argv the launcher builds for `compose`. A test asserts that no path outside the repo is needed (no `/opt/seshat/...` or `/tmp/...` literal in the modules). |
| AC-2 scorer fails a pooled pass | `test_scorer_fails_pooled_pass`: rows with 26/30 decline-correct and 48/48 expand-correct. The verdict must be FAIL and the decline row must read FAIL. |
| AC-3 reproduces the measurement | Live run on the local binding with thinking off. Decline-correct inside 89–100% (30/30) and expand-correct inside 86–99% (46/48). Needs the owner's OK (about 100 llama.cpp calls). |
| AC-4 digest placement | `test_digest_goes_between_history_and_query`: history, then digest, then query. The system prompt is byte-identical with and without a digest. |

## Files

All new, under `scripts/eval/fre1537/` unless noted.

| File | Role |
|------|------|
| `followups.yaml` | The 8 two-turn fixtures, copied unchanged from the archive. |
| `fixtures.py` | Loads the 19 fixtures of `scripts/eval/fre1498/fixtures.yaml` and the 8 follow-ups, with the expected decision of each. `c5_noverb` is excluded. |
| `stub.py` | Recording stub for the model endpoint. Runs inside a container. Stdlib plus fastapi and uvicorn. |
| `gateway.py` | `build`, `up`, `render`, `down`. Standalone `docker run`, own names, joins the network of the eval substrates. Never `docker compose`. |
| `capture.py` | Drives one real eval-gateway turn per fixture and saves the primary's request body. |
| `render.py` | Renders the design A planner request with the production functions. Optional digest. Runs inside the gateway container for the live step. |
| `replay.py` | Decision arm and timing arm, direct to llama.cpp. One JSON row per call. Resumable. |
| `longhist.py` | Long-history arm at 8,000, 30,000 and 60,000 characters. |
| `fingerprint.py` | Engine and build, model and quant, planner mode parameters, system prompt hash. |
| `score.py` | Scorer. Wilson intervals, separate rates, every D7 threshold with PASS or FAIL. |
| `README.md` | One command per step. |
| `.gitignore` | One line for `telemetry/evaluation/fre1537-planner-probe/`. Run output and captured bodies stay out of git. |
| `tests/test_eval/test_fre1537_*.py` | Offline tests. No LLM, no Docker. |

## Design decisions

1. **Standalone containers.** Names `fre1537-stub` and `fre1537-gateway`. Image tag `seshat-gateway:fre1537`,
   built with `docker build` (never the production tag). The network is read from the running
   `cloud-sim-postgres-eval` container, so no network name is hard-coded. `up` stops if any of the four eval
   substrate containers is missing. Every URL in the gateway environment must name an `-eval` host or the stub.
   A test enforces it. Secrets travel as `-e NAME` with the value in the docker CLI's own environment, never in argv.
2. **Files written by a container.** The stub runs as the calling user (`--user uid:gid`), so its output in the
   run directory is not root-owned.
3. **Render inside the gateway.** The production functions read settings. The container holds the settings of the
   image that production runs, so the live step renders there with `docker exec`. The functions are plain imports,
   so the unit tests call them on the host.
4. **Decline rule.** Production code has no decline rule yet (FRE-1541 adds it). The probe inserts the ADR-0152
   rule as the archive did, with the same anchors and the same assertions. The prompt hash covers the result.
5. **Digest.** `build_user_message(history, digest, query)` gives `Conversation so far`, then the digest text, then
   `Strategy: HYBRID\nQuery: …`. The system prompt never depends on the digest.
6. **Planner modes.** `thinking_off` (`chat_template_kwargs.enable_thinking: false`, the D4 configuration) and
   `server_default` (today's production setting). Sampling comes from the captured primary request.
7. **Timing arm.** Protocol T without design B. Per fixture: prime, primary alone (T1), prime, planner then
   primary (T3). Removing B also removes the cache artifact of A4 (B ran before A on the same prefix).
8. **Scoring rules.**
   - A parse failure is not a valid decline and not an expansion, so it is incorrect on both. It is also counted
     on its own line.
   - A threshold is "at least k of n". If the rows hold fewer than n draws, the threshold is FAIL, not scaled.
   - The p50 is the value at index `round(0.5·(n−1))` of the sorted values, as Appendix A4 states.
   - The reasoning-characters threshold covers every call of the run. A run with thinking on fails it. That is
     the intent of D4 and AC-7.
   - A missing fingerprint field (engine build, quant) makes the verdict FAIL. D7 Requalification needs it.
   - The report prints no pooled or weighted figure. A test asserts it.
9. **Exit code.** `score` exits 1 when any threshold is FAIL.

## Steps (TDD: the test first, watch it fail, then the code)

1. **Scorer.** `test_fre1537_score.py` then `score.py`.
   Run: `uv run pytest tests/test_eval/test_fre1537_score.py -q`.
   Cases: AC-2 pooled; all 9 thresholds at the boundary (28/30 PASS, 27/30 FAIL; 43/48 PASS, 42/48 FAIL;
   11/12 PASS, 10/12 FAIL per direction); parse failure; reasoning character; completion p50 41; declined
   p50 2.1 s; long-history 10.1 s and 60.1 s; short sample; missing fingerprint; Wilson 30/30 = 89–100 and
   46/48 = 86–99; no pooled figure in the report.
2. **Fixtures and render.** `test_fre1537_render.py` then `fixtures.py`, `followups.yaml`, `render.py`.
   Cases: 19 + 8 fixtures; expected counts (10 decline, 16 expand scored, `c5_noverb` excluded); AC-4 order and
   system prompt identity; decline rule present and `SINGLE` in the schema; stable system prompt hash;
   anchors missing raises.
3. **Replay core.** `test_fre1537_replay.py` then `replay.py`, `longhist.py`, `fingerprint.py`.
   Cases: `parse_plan` (fenced JSON, `</think>` prefix, garbage, `SINGLE` with tasks); `stream` against a local
   fake SSE server (httpx `MockTransport`) for reasoning characters, first token and usage; resume skips done
   rows; the request body of mode `thinking_off` carries `enable_thinking: false`.
4. **Stub, capture, launcher.** `test_fre1537_capture.py`, `test_fre1537_gateway.py`, then `stub.py`,
   `capture.py`, `gateway.py`.
   Cases: stub records bodies and answers per `tools`; `primary_body` needs exactly one hit; env builder has no
   production host, no secret in argv; argv has no `compose`; AC-1 source scan.
5. **README and `.gitignore`.**
6. **Live capture and render (stub, no LLM).** `gateway up`, `capture`, `render`, `gateway down`. Compare the
   captured primary with Appendix A1 (16 tools, 9,779-character system prompt). A difference is reported, not
   hidden. After `up`, check `docker inspect cloud-sim-searxng` mounts are unchanged.
7. **Gates.** `make test` (scoped first), `make mypy`, `make ruff-check`, `make ruff-format`,
   `pre-commit run --all-files`. Commit. Self-review on `git diff origin/main...HEAD`.
8. **AC-3 (owner's OK first).** Ask the owner. Then `replay --mode thinking_off`, `longhist`, `score`. Post
   the scorer output on FRE-1511 in the handoff.

## Codex plan review, round 1 — changes made

| Finding | Change |
|---------|--------|
| Single-turn p50 could be masked by follow-ups | Already the design: the p50 takes only `kind == single` declines. A test pins it (`test_follow_up_declines_do_not_count_toward_the_single_turn_time`). The plan table now says so. |
| Launcher could remove a same-named container, retag an image, or join a shared network | Containers and the image carry the label `fre1537.probe=1`. `up` refuses a fixed name that exists without the label. `down` removes only labelled containers. The image tag is `fre1537-probe-gateway:<build fingerprint>`, never a production tag. The network must hold all four eval substrate containers, and exactly one network must. `up` compares the `cloud-sim-searxng` container id and start time before and after, and stops on a difference. |
| Environment unspecified | Section "Gateway environment" below. |
| AC-1, AC-3, AC-4 proofs admit false positives | AC-1: step 6 runs every documented command live, and the handoff quotes the output. AC-3: the fingerprint records the captured primary request (tool count, tool-name hash, system prompt length), and step 6 compares it with Appendix A1 (16 tools, 9,779 characters). AC-4: production has no digest builder before FRE-1541, so the test pins the probe's builder. The history and the system prompt come from the production functions. README and handoff state that FRE-1541 must switch the probe to its builder. |

### Gateway environment

Fixed values: `AGENT_DEPLOYMENT_PROFILE=eval`, `APP_ENV=eval`, `API_HOST=0.0.0.0`, `API_PORT=9001`,
`AGENT_GATEWAY_AUTH_ENABLED=false`, `AGENT_MCP_GATEWAY_ENABLED=false`,
`AGENT_PRIMITIVE_TOOLS_ENABLED=true`, `AGENT_PREFER_PRIMITIVES=true` (as production; the eval compose sets
both false), `AGENT_DELEGATION_ENABLED=false` (production default; the eval compose sets true),
`AGENT_EXPANSION_ENABLED=false`, `AGENT_SLM_BASE_URL=http://fre1537-stub:8700`,
`AGENT_ELASTICSEARCH_URL`, `AGENT_NEO4J_URI`, `AGENT_NEO4J_USER` and the two database URLs, all on `-eval`
hosts, and `AGENT_OWNER_STORAGE_ALLOWLIST` as in the eval compose. The event
bus stays off (its default): a capture turn must write nothing to a knowledge graph.
From the caller's environment (secrets as `-e NAME`): `POSTGRES_PASSWORD`, `NEO4J_PASSWORD`,
`AGENT_OWNER_EMAIL`, and `SESHAT_APP_EVAL_PASSWORD` (optional). Prompt-shaping keys pass through when set:
`AGENT_SKILL_ROUTING_MODE`, `AGENT_OWNER_NAME`, `AGENT_CONVERSATION_MAX_HISTORY_MESSAGES`,
`AGENT_LOCATION_ENABLED`. The capture driver posts bare HTTP turns, not `IsolatedArmRunner` turns, because the
gateway has no memory graph and no bus, and the model is a stub. README states this.

## Risks

- A standalone gateway may fail its boot guards (storage allowlist, eval isolation validator). The environment is
  built from `docker-compose.eval.yml`. Step 6 shows any gap.
- Rendering on a settings set that differs from production changes the prompt hash. Step 3 of the design handles it.
- AC-3 may miss the interval. Then the port changed the instrument. Report it, do not tune the thresholds.
