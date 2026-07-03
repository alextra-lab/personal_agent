# FRE-432 Phase 0 — Do trivial turns burn heavy thinking on the primary?

> **Date:** 2026-07-03
> **Ticket:** [FRE-432](https://linear.app/frenchforest/issue/FRE-432) Phase 0 (measurement)
> **ADRs:** ADR-0082 (tier-aware selection), ADR-0084 (pedagogical architecture — the counterweight)
> **Instrument:** `scripts/research/fre432_ph0_thinking_probe.py` (standalone, read-only + serial replay)
> **Substrate:** live `cloud-sim` stack (route_traces `:5432`, ES `:9200`, primary via `slm.frenchforet.com`)

## Verdict

**CONFIRMED (with a measurement caveat).** The M2 hypothesis — *"primary thinking-token
usage on trivial turns is actually high"* — holds. On `SINGLE`-strategy `conversational`
and `memory_recall` turns the primary generates a **median of 816 completion tokens**, and
that generation is **thinking-dominated (~75% of characters are `<think>`)** even for the
most trivial stimuli. The exact *live* thinking share cannot be measured directly with
today's telemetry (no durable think/visible split; live prompt not stored), so the precise
figure is bracketed, not pinned — but every instrument that can see the truth agrees the
mass is large.

> ⚠️ **A first-pass proxy said the opposite.** Reading `route_traces.output_tokens` alone
> gives a median of **125** tokens/turn and suggests *light* thinking. That is an
> **artifact of a ledger undercount** (see §4). Going direct to the thinking model — as the
> owner directed — is what corrected it. Measure, don't assert.

## 1. Hypothesis under test

ADR-0082's reconceived scope gates any Phase-2 routing change on M2: *"[the ledger] must
confirm that primary thinking-token usage on trivial turns is actually high (hypothesis:
yes) before routing."* Phase 0 is that test — confirm or refute with evidence, before code.

Population: turn-level route-trace rows (`task_id IS NULL`) with
`decomposition_strategy = 'single'` and `task_type ∈ {conversational, memory_recall}`.
**n = 168** turns, 66 sessions, all `CHAT`/`NORMAL`, 2026-06-07 → 2026-07-03 (real traffic,
not a synthetic eval batch).

## 2. Backend-aware truth: nothing durably records "thinking tokens"

The quantity of interest is not a first-class field anywhere:

- **`route_traces`** (FRE-452 ledger) records `output_tokens` (`SUM(api_costs.output_tokens)`)
  and `final_reply_chars`, but no think/visible split. `thinking_enabled` is **NULL on all
  168 rows** — never populated.
- **`model_call_completed`** (ES, per model call) records `input/output/total_tokens` — again
  no reasoning split.
- **`reasoning_content_length`** exists on only **18 docs** in all history — the
  `captains_log` *reflection* callsite, not user-facing turns.

The deployed primary **does** run with thinking enabled (`models.cloud.yaml`:
`thinking_budget_tokens: 32768`, no `disable_thinking`; the sub-agent has
`disable_thinking: true`). So the premise is genuinely testable — the model *can* think; the
question is whether it *does* on trivial turns.

Because no field measures it, the probe triangulates three instruments:

```
  ledger proxy            live authoritative           direct replay
 (route_traces)          (ES model_call_completed)   (re-hit thinking model)
 output_tokens            output_tokens                reasoning_content split
   UNDERCOUNTS      <     TRUE total generation   ~    UPPER BOUND on thinking
   (broken)               (no think split)             (no grounding context)
       └──────────── the truth is bracketed in here ───────────┘
```

## 3. Method

`scripts/research/fre432_ph0_thinking_probe.py` (pure helpers unit-tested in
`tests/scripts/test_fre432_ph0_thinking_probe.py`):

1. **Ledger proxy** — read `output_tokens` for the 168 target turns from `route_traces`.
2. **Live authoritative** — sum `model_call_completed.output_tokens` (role=`primary`) per
   trace from `agent-logs-*`.
3. **Direct replay** — recover 12 text-only stimuli from `agent-captains-captures-*`
   (`user_message`, vision turns excluded), sampled evenly across the ledger token range,
   and **serially** re-send each bare stimulus to the primary (`temperature 0.6`, `top_p
   0.95`). Parse the raw `reasoning_content` vs `content` and compute the char think-share.
   Replays are serial because the primary is single-concurrency (`max_concurrency: 1`) and
   parallel calls contend with live traffic. No `/chat`, no KG/memory write.

## 4. Results

### 4a. The ledger is a broken proxy — it undercounts 45% of turns

| Instrument | median | p90 | max |
|---|--:|--:|--:|
| `route_traces.output_tokens` (proxy) | **125** | 1829 | 25481 |
| ES `model_call_completed` (authoritative) | **816** | 2341 | 5242 |

**75 of 168 turns (45%)** have `route_traces.output_tokens < ES sum` — the ledger dropped
`api_costs` rows for some model calls. Example: trace `a8cb5bdb…` ("Are you multimodal?")
records `output_tokens=9` in the ledger, but ES shows two primary calls (`out=9` then
`out=221`). The routing decision, had it read the ledger, would have seen ~1/6 of the real
generation. **The ledger must not be used to gate routing.**

### 4b. Live generation is large — and larger for the "trivial" class

| task_type | n | median | p90 | max |
|---|--:|--:|--:|--:|
| conversational | 157 | **854** | 2374 | 5242 |
| memory_recall | 10 | 580 | 1103 | 1103 |

### 4c. That generation is thinking-dominated (direct replay, upper bound)

12 serial replays, **median think-share 75%** (range 66–87%), estimated **296–1316 thinking
tokens per turn**. The share does **not** fall for trivial stimuli:

| stimulus (bare) | completion tok | think-share | est. think tok |
|---|--:|--:|--:|
| "Better now?" | 359 | 82% | 296 |
| "peal the eggplant?" | 922 | 74% | 684 |
| "Do you know my name and where i live?" | 472 | 81% | 382 |
| "How long do you keep durable notes?" | 1120 | 70% | 785 |
| "Why would i trust your assessment…" | 1716 | 77% | 1316 |

## 5. Resolving the instrument disagreement

Two forces, both real, both now measured:

1. **Ledger undercount (§4a)** — pushed the proxy median down to 125. Direction: the proxy
   *underestimates*. Magnitude: ~6.5× at the median.
2. **Grounding suppression** — the bare replay of "Are you multimodal?" generated 599 tokens;
   the *live* turn (with ~11k tokens of system prompt + memory injected) generated ~230.
   Direction: the bare replay *overestimates* live thinking. So the 75% share is a **ceiling**,
   not the live figure.

Netting them: live total generation is authoritatively **~816 tokens median**, and that
generation is thinking-heavy. Even under a conservative discount for grounding (assume live
think-share is only *half* the bare 75% → ~37%), that is still **~300 thinking tokens per
trivial turn at the median**. The lower bound clears "high."

## 6. Implication for ADR-0082

- **The cost premise holds.** There is a real, large thinking-token mass on trivial SINGLE
  turns (contradicting the "MoE sparse activation already suppresses thinking" conjecture in
  the ticket's finding #3). Phase-2 exploration of a delegation/thinking-policy is
  **justified** — there is something to save.
- **The pedagogical counterweight still governs the decision.** Confirming *"there is
  thinking to reduce"* is **not** license to route trivial turns off the primary. ADR-0084's
  finding #1 (primary is the Socratic continuity layer) stands: Phase 2 must A/B any
  thinking-policy for **quality-neutrality against the primary baseline** before any default
  flip, exactly as the ticket already gates it.
- **Fix the instrument first.** Phase 2 cannot be A/B'd on cost if the cost ledger undercounts
  45% of turns and nothing records the think/visible split. The two follow-ups below are
  prerequisites for a measurable Phase 2.

## 7. Follow-ups filed (Needs Approval)

1. **`route_traces` / `api_costs` output-token undercount** — 45% of turns record fewer
   `output_tokens` than ES; the ledger drops `api_costs` rows for some model calls. Root-cause
   the missing rows and reconcile ledger ↔ ES.
2. **Durable reasoning-token split** — add a `reasoning_tokens` / think-vs-visible field to
   `model_call_completed` (and surface on `route_traces`) so live thinking share is directly
   measurable without replay.

## 8. Reproduction

```bash
# live stack; do NOT set APP_ENV=test (redirects to the empty test substrate)
set -a; source /opt/seshat/.env; set +a   # CF Access service token + DB password
uv run python scripts/research/fre432_ph0_thinking_probe.py \
    --pg-dsn "postgresql://agent:${PW}@localhost:5432/personal_agent" \
    --es-url  http://localhost:9200 \
    --slm-url https://slm.frenchforet.com/v1 \
    --replay-n 12 --out /tmp/fre432_ph0.json
# --no-replay runs the ES distribution only (no inference, no CF token needed)
```

## References

- ADR-0082 (`docs/architecture_decisions/ADR-0082-*.md`), ADR-0084 (pedagogical architecture)
- FRE-452 route-trace ledger — `src/personal_agent/observability/route_trace/`
- Model config — `config/models.cloud.yaml` (primary `thinking_budget_tokens: 32768`)
- Adapter think-stripping — `src/personal_agent/llm_client/adapters.py`
- Methodology precedent — `docs/research/fre374-provenance-perf-probe-results.md`
