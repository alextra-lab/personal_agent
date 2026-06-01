# FRE-433 — cross-turn KV-cache A/B

Compares two prompt layouts on the same live stack, for both backends:

- **arm A (`head`)** — volatile block (recalled memory + skill bodies) in the system HEAD. Current `main`.
- **arm B (`tail`)** — volatile block at the message TAIL. Gateway running branch `codex/fre-433-layout-tail-arm` with `AGENT_CACHE_VOLATILE_TAIL_LAYOUT=true`.

The arm is set by **what's deployed**; the harness only tags `--arm`. The backend is per-request via `--profile {local,cloud}` — no redeploy to switch backends.

## Headline metric
`cache_read_tokens` on the **first full-context** model call of each turn ≥ 2 (cross-turn reuse).
- Arm A expectation: ~0 (full re-prefill every turn).
- Arm B expectation: > 0 (reuses the stable prefix). Test 3 (offline) showed `prompt_n` 6799→277, `cache_n` 0→6771.

Also watch (quality gate): per-turn answer quality (FRE-407). Arm B relocates skill bodies/memory to a trailing user message — answers must stay flat-or-up.

## Run protocol (4 passes, 1 redeploy)

> ⚠️ **Single-slot `:8502`.** The `--profile local` passes hit the one physical KV slot the slm_server session tests on. **Serialize** — do not run the local pass while that session is exercising `:8502`, or the slot thrash corrupts both. The `--profile cloud` passes have no contention.

> ⚠️ **Shared gateway.** Switching arm A→B is a cloud-sim gateway redeploy onto Codex's branch + flag. Get explicit go before deploying; revert to `main` after.

```bash
EMAIL=<loopback-eval-email>          # CF-Access user to impersonate
RUN=ab-$(date +%Y%m%d)

# --- ARM A: current main (flag off) ---
uv run python scripts/eval/fre433_cache_ab/harness.py --run-id $RUN --arm head --profile cloud --auth-email $EMAIL
#   (coordinate with slm_server, then:)
uv run python scripts/eval/fre433_cache_ab/harness.py --run-id $RUN --arm head --profile local --auth-email $EMAIL

# --- redeploy gateway onto codex/fre-433-layout-tail-arm with the flag ---
#   on the VPS: AGENT_CACHE_VOLATILE_TAIL_LAYOUT=true, then
#   ENV=cloud make rebuild SERVICE=seshat-gateway   (needs owner approval)

# --- ARM B: tail layout (flag on) ---
uv run python scripts/eval/fre433_cache_ab/harness.py --run-id $RUN --arm tail --profile cloud --auth-email $EMAIL
#   (coordinate with slm_server, then:)
uv run python scripts/eval/fre433_cache_ab/harness.py --run-id $RUN --arm tail --profile local --auth-email $EMAIL

# --- revert gateway to main; diff the four *.md / *.json in telemetry/evaluation/fre433-cache-ab/ ---
```

## Output
`telemetry/evaluation/fre433-cache-ab/<run>_<arm>_<profile>.{json,md}` (gitignored telemetry dir).
Each pass prints a per-turn table + a cross-turn reuse rollup. PASS for arm `tail` = most turn≥2 calls show `cache_read > 0` vs ~0 on arm `head`, with FRE-407 quality flat-or-up.

## Notes
- `--chat-url` defaults to `http://localhost:9001/chat` (cloud-sim). Use `:9000` for `make dev`.
- Sessions are ≤4 turns so within-session compression doesn't fire (out of A/B scope; that's the D2/D3 design question the A/B informs).
- The harness only POSTs `/chat` and READS ES — it never writes substrate directly (FRE-375 clean). Driving `/chat` does create real sessions/memory under the run-id tag; that's inherent to an end-to-end eval.
