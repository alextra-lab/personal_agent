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

## PR-B — Wire insertion hook + expand tool + pinning (next build task)

### B1 — Per-round insertion-time digest pass (D1)
In `executor.py` immediately **before** `ctx.messages.extend(tool_results)` (line ~3454): a pass
over the accreted tail that (1) digests newly-arrived oversized, non-pinned, non-excluded results
*at birth* (verbatim bytes never enter `ctx.messages`), and (2) re-evaluates deferred pins from
prior rounds and digests any whose release condition is met. Gated by
`settings.tool_result_compression_enabled`. R2 puts run concurrently (batch), digest substitution
awaits them bounded by `tool_result_digest_put_timeout_ms`; timeout/failure → leave verbatim + log.
Threads `ctx.trace_id`/`ctx.session_id` (ADR-0074).

### B2 — D4 retention & dependency pinning
`tool_result_digest_keep` most-recent results verbatim; pin the most-recent `read` of a path while
a dependent `edit`/`write` against that path is pending. Release on first successful `edit`/`write`
against the path **or** `pin_ttl_turns` elapsed (failed edit/write does **not** release — Codex
B3). Pin state lives on `ExecutionContext`.

### B3 — `expand_tool_result(key[, offset, limit])` tool (D5)
New Tier-1 native tool (`tools/` + register in `tools/__init__.py` alongside the R2-gated artifact
tools + `config/governance/tools.yaml` entry). Fetches full bytes from R2 for the digest key,
**hash-validated** for exact replay, **ranged** retrieval default for large outputs, capped by
`tool_result_digest_max_expand_tokens`. **(Codex Q5 — MED) Ranged retrieval has no storage support
yet:** `R2ArtifactStore.get` (`artifact_store.py:222`) always fetches the whole object. PR-B either
adds a `Range`-aware get to the store, **or** fetches full bytes then slices locally — decide in
PR-B and document the memory/latency tradeoff (full-then-slice is simplest and the cap bounds the
returned size). Re-expansion tokens tracked on the separate `tool_result_digest_reexpanded`
telemetry dimension. Contract kept **separate** from the future `recall_session_history`
(FRE-465) — converge vocabulary/UX only.

### B4 — Transcript + reasoning invariants (D6, correctness)
- Assert immediate `tool_use`→`tool_result` adjacency for every digested message (exact position,
  same `tool_call_id`, no reordering) — stronger than orphan-absence.
- Honor a per-provider-adapter "do-not-compress floor" (`llm_client/`): the compressor receives an
  already-computed floor index; reasoning items the API requires intact are not compressed.
- Fold digested messages into ADR-0081's send-time byte-fixed-point checks (`/no_think` stripping,
  role-fixing, `_sanitize_tool_pairs`).

### B5 — Follow-up ticket
File **D6 lost-in-the-middle digest pinning** as a new Needs-Approval issue under the *Turn Cost &
Latency Optimization* project.

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
