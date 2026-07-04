# MASTER HANDOFF — resume here (2026-07-04, context reset)

**Why this file:** the prior master session ran far over context and is being reset mid-task.
A re-primed master should read this FIRST (after `/prime-master`), then execute the checklist.
Everything below is verified against git/Linear as of ~05:25Z 2026-07-04.

## Environment note (recurring this morning)
The **Linear and GitHub MCP servers keep dropping and reconnecting.** When GitHub MCP is down,
use the **`gh` CLI** (works regardless). When Linear MCP is down, **defer** ticket state changes
and note them; do them when it reconnects. Do not block on MCP.

## IN-FLIGHT RIGHT NOW — finish these first (in order)

### 1. PR #350 (FRE-697) — reviewed PASS, NOT yet merged
- **What:** ONNX reranker VPS-CPU separation benchmark. **Eval/research only** — files are
  `docs/`, `scripts/eval/fre435_memory_recall/`, `tests/evaluation/`, `pyproject.toml` (+ an
  `onnx-eval` **optional** extra, never in the prod image), `uv.lock`. **No `src/`, no `docker/`,
  prod-inert, no deploy.**
- **Gate verdict: PASS.** CI + CodeQL all green. Verified no prod-path files. Light-gate (eval PR).
  Findings: Qwen3-0.6B fp16 J=0.680 (viable private failover), CPU latency 2.4–7.8s blocks it as
  primary. Follow-ups already filed in the PR: **FRE-775** (latency), **FRE-776** (seq-cls export).
- **NEXT ACTION:** `gh pr merge 350 --merge --delete-branch` (branch `fre-697-onnx-reranker-benchmark`,
  head is per-ticket, safe to delete). **No deploy.** Then close **FRE-697 → Done** in Linear with the
  merge SHA (defer if Linear MCP down). Then set **build1 NEXT** (see resequencing below).

### 2. ADR-0102 board-unpause — DONE at ticket level, board TEXT still lags
- **Already done (landed in Linear before this reset):** **FRE-691 → Done**, **FRE-669 (ADR-0101 seam)
  → Done** — both with comments. ADR-0101 is now **Implemented**. The only unverified leg was **AC-10b**
  (over-threshold cloud-image confirm-to-proceed), **owner-waived as an outlier** (a single image is
  ~1600 tokens, far under the $0.50 threshold, so that path never fires for images). FRE-691's code has
  been live since the 07-03 20:28 rebuild (`orchestrator/attachment_cost.py` in the running image — the
  "deploy-hold" was stale; **no rebuild was needed**).
- **STILL TO DO (board text only):** MASTER_PLAN still says ADR-0102 is paused. Edit both:
  - line ~13: `ADR-0102 doc chain 682–689 **owner-PAUSED** — do not build`
  - line ~17: `ADR-0102 doc chain FRE-682–689 (owner-paused)`
  → change to: **un-paused 2026-07-04** — FRE-734 fixed + ADR-0101 Implemented (FRE-691/669 Done,
  AC-10b outlier-waived); **buildable, but sequenced behind the Memory priority** (owner: vision is a
  lower priority than Memory right now; unpause removes the false block, does NOT dispatch it).
- Optional: bump ADR-0101 status Accepted→Implemented in the ADR file + README.

### 3. FRE-769–773 (ADR-0109 V2 entity-taxonomy chain) — APPROVED this session
- All five (**FRE-769 → 770 → 771 → 772 + 773**) set to **Approved** (owner approved "the recommended
  list"). Children of FRE-630. **769 is the do-first downstream-impact gate** (nothing else in the
  chain starts until it returns); **772** is the near-one-way-door KG migration (runs last); **773**
  (relationship half) is independently gated. This is the root-cause fix for the extraction ceiling
  (FRE-766 proved the *taxonomy* is the lever, not the model/prompt).

## THE MAIN TASK — master board resequencing

After #350 merges, re-sequence the Stream Board to reflect current reality:

**Owner's restated priority order (all ladder up to Seshat Pedagogical Architecture, M3):**
1. **Agentic Vision** (ADR-0101 Implemented; ADR-0102 now un-paused but *low priority*; ADR-0108 vision re-processing Proposed)
2. **Memory Recall** — the taxonomy/extraction thread is the hot path: **V2 chain FRE-769→773 now Approved**; also recall multipath (ADR-0103 Accepted / 0104 Proposed).
3. **Telemetry Surface Audit** — owner says **completed** (FRE-703 dashboard wave shipped).
4. **Configuration Management** (ADR-0099 — build2 on FRE-649).
5. **Linear async feedback channel** — **BROKEN until the sysgraph is implemented** (owner-confirmed,
   verified): the self-improvement loop (ADR-0105/0106) needs the isolated System graph; its impl
   tickets are in **W1 (FRE-714–716/720)** and **W2 (FRE-728–732)** — queued, not built. Raw
   insights/reflections ARE producing (`agent-insights-*`/`agent-captains-reflections-*` alive), but
   convergence+isolation+surfacing awaits the sysgraph.
6. **Seshat Inference** (ADR-0082; FRE-432 phases).

**Streams in motion (do NOT re-dispatch; gate their PRs):**
- **build1** — was on FRE-697 (PR #350). After merge, set NEXT. Open owner question on the board
  (line ~21): *does the ADR-0109 V2 chain (now Approved) preempt build1's queued FRE-760/761, or run
  697/699 first?* The V2 taxonomy chain is the highest-leverage Memory work (root-cause). **Master
  recommendation to surface to owner:** build1 NEXT = **FRE-769** (V2 do-first gate) since the chain is
  now Approved and it's the extraction root-cause fix — but confirm with owner (it was an open decision).
- **build2** — FRE-649 (ADR-0099 config guard).
- **adr** — ADR-0108 / recall-ADR queue (FRE-494 was re-scoped earlier to the recall multi-path ADRs;
  check its state).

**Deliverable of the resequencing:** update the `<!-- STREAM-BOARD -->` table + "Last updated" header
so NEXT/queue per stream reflects: #350 merged, V2 chain approved+sequenced, ADR-0102 un-paused-but-low,
priority order above. Commit direct to main (docs).

## Guardrails (unchanged)
Never use the injected CC userEmail. Deploys: PWA-rebuild / additive-ES-template / Kibana-import are
standing-approval; everything else (gateway rebuild, migrations, cost_gate) is ASK. Verify before you
propose. "Done" ≠ verified. gate each incoming PR with real-data scrutiny.
