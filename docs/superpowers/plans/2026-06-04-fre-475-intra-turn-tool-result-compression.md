# FRE-475 — Intra-Turn Tool-Result Compression (ADR-0085)

> **Status:** Plan — awaiting codex review + owner go-ahead to code
> **Ticket:** [FRE-475](https://linear.app/frenchforest/issue/FRE-475) (In Progress · Tier-1:Opus · project *Turn Cost & Latency Optimization (artifact builds)*)
> **ADR:** `docs/architecture_decisions/ADR-0085-intra-turn-tool-result-compression.md` (Proposed, PR #158)
> **Research:** `docs/research/2026-06-04-artifact-turn-cost-latency-forensics.md` (trace `a0a07227`)

## Context

The first successful artifact-build turn after the FRE-469 classifier fix (`a0a07227`,
claude-sonnet-4-6, cloud) was *correct but expensive*: 23 LLM rounds, ~$1.14, 768k full-price
input tokens — ~40% of input-side tokens. Root cause (research §4): each `bash`/`read`/tool
result is appended to the transcript **verbatim** (`executor.py:3431` builds the
`{"role":"tool","content":…}` dict; `executor.py:3454` does `ctx.messages.extend(tool_results)`),
the agentic loop re-sends the growing `ctx.messages` every round, and because `cache_control`
breakpoints are clamped to ≤4 (FRE-468) and pinned to the stable head, the accreting tool-output
**tail lives past the last breakpoint and is re-billed at full price every round**.

ADR-0085 fixes this by compressing large tool results to a deterministic, **byte-stable** digest
**before their verbatim bytes ever enter `ctx.messages`**, persisting the full bytes to R2
(ADR-0069), and leaving an imperative `expand_tool_result(key)` affordance for exact replay.
Intended outcome: flatten the per-round fresh-input curve at its largest source (target ≥30%
total fresh-input reduction) with no artifact-quality regression.

## Scope decisions (owner-confirmed 2026-06-04)

- **Two PRs under FRE-475** (ticket stays *In Progress* until PR-B ships):
  - **PR-A (this session):** digest infrastructure — key builder, format-aware extractors,
    byte-stable digest serializer, R2 persistence helper, config knobs + master flag (default
    **off**), and the byte-stability fixed-point test. **Not wired** into the executor; ships
    behind the flag with full unit coverage.
  - **PR-B (next build task):** wire the per-round insertion-time digest pass into
    `executor.py`, the `expand_tool_result` tool (D5), and D4 retention/dependency-pinning.
- **D6 optional lost-in-the-middle digest pinning** → deferred follow-up ticket (ADR says it is
  "not a gate for the first shippable flag"). D6 *correctness* invariants (tool-pair adjacency,
  reasoning do-not-compress floor) are in PR-B, not deferred.
- **A/B verification (acceptance criteria 3 & 4)** is **post-deploy (master), recorded in a
  Linear comment** — not a PR checklist item (lifecycle PR-hygiene: no post-deploy items in PR
  checklists). These build PRs ship flag-OFF implementation + unit/byte-stability tests only.

---

## PR-A — Digest infrastructure (this session)

New module: **`src/personal_agent/orchestrator/tool_result_digest.py`** (pure, no executor
coupling — easy to unit-test without an event loop). Reuses `R2ArtifactStore`
(`storage/artifact_store.py`) and mirrors the validation discipline of `build_r2_key`.

### A1 — Config knobs + master flag (D7)
`src/personal_agent/config/settings.py` — add, modeled on Anthropic's context-editing vocabulary,
all with descriptions, never hardcoded (place beside the ADR-0061/0081 compression knobs ~line 567):

| Setting | Default | Meaning |
|---|---|---|
| `tool_result_compression_enabled` | `False` | Master kill switch (D7; rollout gate). |
| `tool_result_digest_threshold_tokens` | `1500` | Compress results estimated above this (open decision §1: conservative 1.5–2k). |
| `tool_result_digest_keep` | `3` | Most-recent tool results kept verbatim (Anthropic `keep`). |
| `tool_result_digest_min_savings_tokens` | `500` | `clear_at_least` analogue — skip digest that wouldn't pay off (gates deferred-release case b). |
| `tool_result_digest_pin_ttl_turns` | `4` | D4 abandonment bound for a read→edit pin. |
| `tool_result_digest_put_timeout_ms` | `2000` | D1 insertion-path R2-put latency ceiling; timeout → leave verbatim. |
| `tool_result_digest_exclude_tools` | `[]` (`list[str]`) | Per-tool opt-out. |
| `tool_result_digest_head_lines` / `_tail_lines` | `40` / `20` | bash head/tail retention (D2). |
| `tool_result_digest_max_expand_tokens` | `8000` | D5 re-expand cap (anti-spike). |

### A2 — Canonical key builder (D1)
`build_tool_result_key(session_id: UUID, trace_id: str, tool_call_id: str) -> str`
→ `tool-results/{session_id}/{trace_id}/{tool_call_id}`. Sibling of `build_r2_key` with the same
strict-validation discipline. **(Codex Q4)** Provider-issued `tool_call_id`s (and trace ids) may
contain dots, spaces, `?`, `#`, and other URL-unsafe chars, so do not merely "reject traversal":
either match each segment against an explicit strict regex **or** `base64url`-encode `trace_id` +
`tool_call_id` before joining; `session_id` is a `UUID` (already safe). Raise `ArtifactKeyError`
on any segment that escapes the grammar. Deterministic from three stable IDs, joinable per
ADR-0074, minted once.

### A3 — Format-aware deterministic extractors (D2)
A `digest_tool_content(tool_name: str, content: str) -> DigestBody` dispatcher (deterministic,
**no LLM**), returning the lossy body plus a `format` discriminator.

**(Codex Q3 — HIGH) Explicit precedence.** The executor stores successful tool content as
`json.dumps(result.output)` (`executor.py:2996`), so compiler/test/stack-trace output usually lives
*inside* `bash.stdout`. The dispatcher must therefore: (1) parse the tool JSON first; (2) run the
structured-middle detectors over `stdout`, `stderr`, and `read.content`; (3) only then fall back to
bash head/tail. Head/tail must never run before structured-middle sniffing, or it preserves the
wrong region.

Per-tool:

- **`bash`** — parse the JSON (`{success, exit_code, stdout, stderr, command, truncated_path, note}`);
  retain `exit_code`, `command`, `note`, `truncated_path`, stderr header lines, any upstream
  truncation marker, and head (`head_lines`) + tail (`tail_lines`) of stdout with an elision marker.
- **`read`** — keep outline (structure / marker / path / offset / limit) + the matched/ranged
  region the read targeted; drop bulk. (Read already encourages grep-then-range, FRE-410.)
- **Structured-middle formats** (diffs/hunks, stack traces, compiler/test output) — format-aware
  parsers retaining failing file/line/function frames + nearby hunk context (head/tail masking
  would preserve the wrong region — Codex B-HIGH). Detected by content sniffing.
- **JSON results** — type-specific extraction (key paths, counts, matched values, error fields),
  richer than ADR-0061's shape-only `keys=…`.
- **Error payloads** — kept **verbatim** (matches `_content_is_error_payload` heuristic in
  `context_compressor.py`); the model needs the full failure to recover. (Digest is a no-op.)
- **Markup/XML/unrecognized** — explicitly out of scope for a structural extractor (ADR D2);
  fall back to head/tail-with-elision + preserved truncation markers.

### A4 — Byte-stable digest serializer (D3)
`build_digest_message(*, tool_call_id, tool_name, r2_key, content_hash, body) -> dict` produces the
`role="tool"` replacement message. Invariants:
- Serialized as **canonical JSON, sorted keys**; **no volatile bytes** — no timestamps, retry
  counts, presigned/expiring URLs, or non-deterministic ordering. Extractor bodies must also emit
  deterministic ordering (sorted keys; no float formatting drift).
- Embeds the canonical `build_tool_result_key` value + a `content_hash` = **full
  `hashlib.sha256(full_content_bytes).hexdigest()`**, minted once. **(Codex Q2 — HIGH) Do NOT use
  `stable_hash`** (`loop_gate.py:96` — 16-hex, `default=str`); it is for loop dedup, not content
  identity.
- `content` carries the imperative placeholder (D4). **(Codex Q2 — MED) The size shown is the
  exact `len(full_content_bytes)` (a stable byte count), not an estimated token count** — an
  estimator could drift on regeneration. *"Full output hidden (N bytes). Call
  `expand_tool_result("<key>")` to retrieve verbatim before editing against omitted lines."*
- Preserves `tool_call_id`, `role="tool"`, `name` so `context_window._sanitize_tool_pairs` never
  orphans it (D6 adjacency — asserted in PR-B's wiring tests).

### A4b — Pure eligibility helper (so PR-A is self-testable) — **(Codex Q1 — MED)**
`should_digest(*, tool_name, content, keep_index, ...) -> bool` — the *pure* policy predicate:
threshold (`tool_result_digest_threshold_tokens`), `tool_result_digest_min_savings_tokens` floor,
`tool_result_digest_exclude_tools`, and error-payload verbatim check. **Position/recency (`keep`)
and read→edit pinning stay in PR-B** (they need executor/turn state); A4b only owns the
content-intrinsic gates so the PR-A unit tests have a real API to exercise.

### A5 — R2 persistence helper (D1)
`persist_tool_result(store, *, r2_key, content, trace_id) -> bool` — awaits `store.put(...)` to
durable confirmation; on `ArtifactStoreError`/timeout returns `False` (caller leaves the result
verbatim — no digest, no broken pointer). Timeout is enforced by the **caller** (PR-B) via
`asyncio.wait_for(..., put_timeout_ms)`; the helper stays sync-contract-simple. When
`get_artifact_store()` returns `None` (R2 unwired), digestion is a no-op end-to-end.

### A6 — Digest telemetry record
`src/personal_agent/telemetry/tool_result_digest.py` mirroring
`telemetry/within_session_compression.py`: a frozen `ToolResultDigestRecord`
(`trace_id`, `session_id`, `tool_name`, `tool_call_id`, `bytes_in`, `tokens_in`, `tokens_out`,
`format`, `persisted`, `r2_key`, `content_hash`) with `record_digest(record, bus)` dual-write
(durable JSONL + bus), threaded with `trace_id`/`session_id` per ADR-0074. **(Codex Q5 — LOW) The
mirror is only complete with the event-model side:** add the stream constant + typed
`ToolResultDigestEvent` schema in `events/models.py` (as `WithinSessionCompressionEvent` does at
`events/models.py:134,798`). A separate `tool_result_digest_reexpanded` dimension is added in
PR-B (D5).

### A7 — Tests (PR-A, TDD — write first, confirm red, implement)
`tests/personal_agent/orchestrator/test_tool_result_digest.py`:
- key builder: happy path + traversal/slash/control-char rejection raises `ArtifactKeyError`.
- each extractor: bash head/tail + exit-code/stderr/note retention; read outline+range; diff/trace
  frame retention; JSON extraction; **error payload kept verbatim**; markup fallback.
- **byte-stability fixed point (D3, release-blocking):** `build_digest_message(...)` is
  byte-identical across repeated calls, across a serialize→parse→re-serialize round-trip (the
  cross-turn Postgres-replay proxy), **and across a full regenerate-from-the-same-raw-content path**
  (Codex Q2 — exercises the extractor + hash + placeholder end-to-end, not just re-serialization).
  Assert no timestamp/url/estimator-derived keys present.
- threshold + `min_savings` gating via `should_digest(...)` (A4b): below-threshold, low-savings,
  excluded-tool, and error-payload inputs are left verbatim.
- R2 unwired (`get_artifact_store() is None`) → no-op; put-failure → verbatim (mock store raising
  `ArtifactStoreError`).

**PR-A quality gates:** `make test-file FILE=tests/personal_agent/orchestrator/test_tool_result_digest.py`
→ then `make test` (module then full) · `make mypy` · `make ruff-check` + `make ruff-format` ·
`pre-commit run --all-files`.

---

## PR-A — STATUS: SHIPPED (PR #160, merged 2026-06-04)

All A1–A7 landed; flag default-off; full suite green. The module
`orchestrator/tool_result_digest.py` is on `main`. PR-B wires it.

---

## PR-B — Wire insertion hook + expand tool + pinning (this session)

All orchestration logic goes in **`orchestrator/tool_result_digest.py`** (extending PR-A's pure
module with the turn-stateful pieces); `executor.py` gets a **single guarded call**. Insertion site:
`step_tool_execution` (`executor.py:3086`), immediately **before** `ctx.messages.extend(tool_results)`
(`executor.py:3454`). Round counter = `ctx.tool_iteration_count`. Only `read` + `write` primitives
exist (no separate `edit`); both carry a `path` arg — so D4 is the **read→write** hazard.

### B0 — ExecutionContext pin state (D4)
Add to `orchestrator/types.py` `ExecutionContext`:
`tool_result_pins: dict[str, ToolResultPin] = field(default_factory=dict)` keyed by `tool_call_id`,
where `ToolResultPin` (frozen) = `(path: str, round_pinned: int)`. Holds reads left verbatim because a
write to their path may still come. (The verbatim content stays in `ctx.messages`, found by
`tool_call_id` on release.)

### B1 — Per-round digest pass (D1) — `apply_intra_turn_digest(...)`
**(Codex Q1 HIGH — data-flow.)** The assembled `tool_results` entries carry only
`{tool_call_id, role, name, content}` (`executor.py:3431`); `success` lives in `dr["success"]`
(`executor.py:3408`) and the `path` argument in `allowed_plans[...]["arguments"]` (`executor.py:3297`).
So in the existing Phase-3 loop, build a **sidecar map** `digest_sidecar: dict[str, dict]` keyed by
`tool_call_id` → `{"tool_name", "success", "arguments"}` (no change to the transcript message shape).

**Semantics (owner decision): keep-window deferred.** The pass runs over **all** `role="tool"`
messages in `ctx.messages` **after** the current batch is appended, and digests only those **older than
the most-recent `tool_result_digest_keep`** tool results. The current batch stays verbatim so the model
can act on its latest output; a result is digested a few rounds later once `keep` newer results exist.
**Honest cache note:** with `keep`≥1 every digest is a *deferred* rewrite (ADR-0085 case b — one bounded
prefix invalidation each, gated by `digest_saves_enough` = `clear_at_least`), not case-a birth-time;
it still flattens the curve because each result is verbatim for only `keep` rounds, not all 23.

New `async def apply_intra_turn_digest(ctx, sidecar, *, trace_ctx, store, bus=None) -> None` in
`tool_result_digest.py`. `sidecar: dict[str, dict]` (current batch only) keyed by `tool_call_id` →
`{"tool_name", "success", "arguments"}`, built in the Phase-3 loop (Codex Q1: `success`/`path` are not
on the transcript message). Called from `step_tool_execution` **after** `ctx.messages.extend(...)` when
`settings.tool_result_compression_enabled` **and** `get_artifact_store()` is not `None`. Each round:
1. **Pin maintenance (D4, from the current batch via sidecar):** a `read` with a `path` arg records a
   `ToolResultPin(path, round)` keyed by its `tool_call_id`. A **prior-round** successful `write` to
   path P releases pins for P. **(Codex Q2)** a same-batch read+write to P does NOT release this round
   (concurrent dispatch, `executor.py:3315`) — defer to next round/TTL. Pins older than `pin_ttl_turns`
   are released (abandonment). A **failed** write never releases (Codex B3).
2. **Eligibility:** enumerate `role="tool"` message indices; protect the last `keep` (the `floor_index`,
   doubling as the conservative D6 reasoning floor). For each tool message **below** the floor: digest
   when `should_digest(msg["name"], msg["content"])` (PR-A), it is not already a digest, and its
   `tool_call_id` is not an active pin. Older messages are digestable from the message alone
   (`name` + `content`) — no retained arguments needed.
3. **Persist + substitute:** `compute_content_hash(original)` + `build_tool_result_key(UUID(session_id),
   trace_id, tool_call_id)`; `persist_tool_result` puts run concurrently (`asyncio.gather`), whole
   substitution bounded by `asyncio.wait_for(..., put_timeout_ms/1000)`; on timeout/failure/
   `ArtifactKeyError` leave verbatim. For persisted, `build_digest_message`, gate on
   `digest_saves_enough`, replace `msg["content"]` **in place** (same position/`tool_call_id`/`role`/
   `name` — D6 adjacency), emit one `record_digest` row. Codex Q3: in-place rewrite keeps the tool-pair
   fields (`_sanitize_tool_pairs` never orphans it) and mutates `ctx.messages` only before the next send.

`executor.py` change (Codex Q4 — no `_bus` in scope; local lookup, bus optional):
```python
ctx.messages.extend(tool_results)
if settings.tool_result_compression_enabled:
    _store = get_artifact_store()
    if _store is not None:
        await apply_intra_turn_digest(ctx, digest_sidecar, trace_ctx=trace_ctx, store=_store, bus=None)
```
(`bus=None` for the first flag — telemetry still durably writes; wiring a real bus is a trivial follow-up
once a bus handle is threaded into `step_tool_execution`.) Flag-off (default) ⇒ branch skipped ⇒
provably zero behaviour change.

### B2 — `expand_tool_result(key, content_hash[, offset, limit])` tool (D5)
New native tool module `tools/tool_result_expand.py` (`ToolDefinition` + async executor), registered in
`tools/__init__.py` **inside the R2-gated block** (alongside `artifact_read`) + a `config/governance/
tools.yaml` entry mirroring `artifact_read` (category `memory_read`, `risk_level: low`,
`requires_approval: false`, modes NORMAL/ALERT/DEGRADED/RECOVERY).
**(Codex Q5 MED) Hash is not reachable from a native tool** (executor passes only `TraceContext`, not
`ExecutionContext`, and the hash lives inside the digest message content). So `content_hash` is an
**explicit required tool argument** — the model copies both `key` and `content_hash` from the digest it
sees. PR-B updates `build_digest_message`'s `hint` (same module) to
`Call expand_tool_result("<key>", "<hash>") …` so the affordance is self-describing (PR-A's
byte-stability tests still hold: deterministic, `r2_key` still in `hint`). The executor fetches full
bytes via `store.get(key)`, computes `compute_content_hash(fetched)` and **asserts it equals the passed
`content_hash`** (integrity/truncation guard; returns a clean error on mismatch), then
**fetch-full-then-slice** by `offset`/`limit` (Codex Q5 decision — simplest; no new R2 `Range` path;
`max_expand_tokens` caps returned size). Emits a `_reexpanded` telemetry row on the separate dimension.
Contract kept distinct from future `recall_session_history` (FRE-465).

### B3 — D6 invariants (correctness)
- **Adjacency** is structural (in-place replacement, B1) — assert it in tests (exact position +
  `tool_call_id`, no reorder/split), stronger than orphan-absence.
- **Reasoning floor (Codex Q6 — documented ADR-0085 §D6 deviation):** `apply_intra_turn_digest` honors
  an explicit `floor_index` (default = recency-`keep` floor) and never digests at/after it; and it only
  ever rewrites `role="tool"` messages, never assistant/reasoning content. ADR-0085 §D6 mandates the
  floor be computed **per-provider adapter** and handed in; PR-B implements the *floor-honoring
  contract* but defers the per-provider computation (default recency floor) to a follow-up — the cost
  win does not depend on it and the digest never touches reasoning blocks. **Flagged for owner sign-off
  below;** the ADR text update (if accepted) is the adr session's job, not this build PR.
- **Send-time stability:** add a test asserting a digested `role="tool"` message survives
  `_sanitize_tool_pairs` + role-fixing byte-identical (folds digests into the ADR-0081 send-time
  contract).

### B4 — Tests (TDD)
Extend `test_tool_result_digest.py` (or a sibling `test_intra_turn_digest.py`): release-on-successful-
write; no-release-on-failed-write; TTL abandonment release; recency-`keep` floor; fresh-read-pinned-
then-released; birth-time digestion of a large bash result (verbatim never enters `ctx.messages`);
put-timeout → verbatim; `expand_tool_result` happy path + hash-mismatch + ranged slice + cap;
adjacency + sanitize-survival. Flag stays default-off; add focused tests that flip it via monkeypatch.

### B5 — Follow-up tickets (filed, Needs Approval)
- **FRE-482** — ADR-0085 §D6 lost-in-the-middle digest pinning (optional enhancement).
- **FRE-483** — ADR-0085 §D6 per-provider reasoning do-not-compress floor + digest bus telemetry
  (PR-B implements the floor-honoring contract with the recency-`keep` default and calls
  `record_digest(..., bus=None)`; the per-provider floor computation and bus publish are deferred).

---

## Verification (this plan)

- **PR-A:** unit suite above all green; byte-stability fixed-point test passing; `make test` /
  `mypy` / `ruff` / `pre-commit` clean. Feature flag default-off ⇒ zero runtime behavior change.
- **PR-B:** wiring/adjacency/floor tests; flag still default-off.
- **Post-deploy (master, Linear comment — not in PR):** deploy flag-on to cloud-sim gateway,
  re-run an equivalent artifact turn, report the per-round `fresh_in` table vs `a0a07227`
  (target ≥30% reduction), side-by-side artifact-quality eval, and backend-aware cache counters
  (cloud `cache_read_input_tokens`) per FRE-433 discipline.

## Notes / blockers at plan time
- **Opus safety-classifier outage** is currently blocking all classifier-gated actions: `git`
  (so the Step-0 worktree reset — fetch / ff-merge `origin/main` / push `worktree-build` — has not
  run), `gh`, MCP, **`codex:rescue` (the mandated plan review)**, and `make test/mypy/ruff`.
  Coding begins only once the classifier recovers (so codex plan-review + TDD red/green + quality
  gates can actually run) and the owner gives the go-ahead.
- Confirm `read` tool output keys against `tools/primitives/read.py` when writing the A3 read
  extractor (outline/marker/offset/limit field names).
