# ADR-0085 — Intra-Turn Tool-Result Compression (Insertion-Time Digest + Exact Re-Expand)

**Status:** **Parked (dormant, flag-off) — 2026-06-05** (was Proposed 2026-06-04). See **Decision Outcome** below. Infra remains in-tree behind `tool_result_compression_enabled=false`; not shipped.
**Related:** ADR-0081 (cache-aware context layout & compaction — **this ADR extends its tiered-context model with an intra-turn tool-result tier**), ADR-0061 (within-session progressive compression — the middle-band `_pre_pass_tool_outputs` this composes with), ADR-0069 (R2 artifact substrate — the durable store for full results), ADR-0074 (identity / joinability — emit-site + trace threading the digest inherits), FRE-468/FRE-473 (cache_control breakpoint clamp — the constraint that makes the tail expensive), FRE-465 (ADR-0081 D5 cold-tier history retrieval — the **converged re-expand vocabulary**, distinct contract)
**Implements:** FRE-475 (project: *Turn Cost & Latency Optimization (artifact builds)*)
**Evidence:** `docs/research/2026-06-04-artifact-turn-cost-latency-forensics.md` (trace `a0a07227`), pre-write Codex design critique (logged in the PR), external landscape (see References)

---

## Decision Outcome (2026-06-05) — Parked

**Decision: park this ADR dormant (flag-off); do not ship intra-turn compression; pivot to FRE-476
(decomposition + sub-agents).** Full rationale: `docs/research/2026-06-05-tool-result-compression-park-decision.md`.

The decisive finding is **architectural, not just a cost number**:

- The digest's only lossy primitive for unstructured streams is **head/tail truncation that deletes the
  middle** (`_headtail`). When the model reads source via **bash** (`cat`/`grep`/`sed`) — which it
  overwhelmingly does — that file content flows through the bash digest path (§D2 bash bullet) and **its
  middle is deleted**. The model reasons over mutilated source, abandons its `cat`/`grep`/`sed` technique
  (live-observed, trace `950386d6`), and ships an incomplete artifact.
- §D2 anticipated range-preserving reads *only on the `read`-tool path* ("`read` already encourages
  grep-then-range, FRE-410"). It never accounted for reads arriving via **bash**, where there is no
  extractor and no pin (§D4) — only head/tail. That is the gap.
- The harness **already solves file-read context-bounding** via the exact technique the digest corrupts:
  the always-injected `bash` skill + the `read` tool's own description instruct the model to **`grep -n`
  to locate → read narrow ranges** (`orchestrator/skills.py:35` injects `docs/skills/*.md` bodies every
  turn). The digest is a **second truncation layer fighting the harness's own read contract.** For the
  artifact-build workload the re-billing tail *is* those file reads, so a correct fix ("don't compress
  them") leaves little to compress and the cost win evaporates.

**Disposition:**
- **Code kept dormant, not removed** — the infra is still useful for large *non-file* outputs
  (JSON/`psql`/`curl`); it carries the worth-keeping fix-a guard (`_NEVER_DIGEST_TOOLS`) and the reusable
  offline harness. Flag description + module docstring marked PARKED-with-reason (anti-rot). Removal is a
  deferred option if FRE-476 obviates compression entirely.
- **`read` tool kept** — it is the path-governed, head-capped, all-modes (incl. LOCKDOWN/RECOVERY where
  `bash` is forbidden) file door; not redundant despite the model preferring bash.
- This is a **multi-phase ticket park**, not a Done: FRE-475 and the blocked enhancements
  (FRE-482/483/485) park with the ADR; FRE-476 becomes the active stream.

Everything below is the **original Proposed design**, retained as the record of what was built and why the
park is a conclusion rather than an abandonment.

---

## Context

### The measured problem

FRE-469 fixed a classifier misroute so artifact/build turns route to `TOOL_USE` (tool budget 25) and the artifact ships. The first successful such turn (`a0a07227`, claude-sonnet-4-6, cloud path) is *correct* but expensive: **23 LLM rounds, ~$1.14, 14 m 34 s, 768 k full-price input tokens** — ~40 % of input-side tokens. Per-round forensics (research doc §4) show the cause precisely:

```
 #  fresh_in  cache_rd   phase
 2    14,803         0   ┐ DISCOVERY — fresh_in climbs monotonically
 8    23,674    35,496   │
12    38,622    56,660   │
18    47,366    91,802   │
22    56,199    94,998   ┘
23    71,214   107,934   assemble + commit
```

Mechanism: each `bash`/`read`/tool result is appended to the conversation transcript **verbatim** (`executor.py:3431` builds the `{"role":"tool","content":…}` dict; `executor.py:3454` does `ctx.messages.extend(tool_results)`). The within-turn agentic loop re-sends the growing `ctx.messages` every round. Anthropic prompt caching only matches a prefix up to a `cache_control` breakpoint; breakpoints are clamped to **≤4** (FRE-468) and placed on the **stable head** (system / skills index / memory / early history). The accreting tool-output **tail therefore lives past the last breakpoint and is re-billed at full price every subsequent round** until it ages into the cached prefix. This is the **intra-turn** analogue of the cross-turn KV-reuse defect ADR-0081 fixed: volatile, growing content sitting where the cache cannot protect it.

### What already exists (and the gap)

- **ADR-0061 `_pre_pass_tool_outputs`** (`orchestrator/context_compressor.py:297`) replaces large `role="tool"` payloads with a one-line *shape descriptor* (`keys=…` / `list[N]`). But it runs only in the **middle band** at **eviction time** during cross-turn compaction, it is **lossier than we want** (shape only, no content), and it has **no retrieval path** — evicted content is gone. It is the *warm/middle* tier, not the *tail at insertion*.
- **ADR-0081 D2/D3** (FRE-434) frozen append-only layout + cache-aware compaction scheduler is **shipped + live**. Its byte-fixed-point discipline and frozen-layout invariant are the foundation this ADR builds on; the gap it leaves open is *intra-turn at insertion time*.
- **ADR-0069 `R2ArtifactStore`** (`storage/artifact_store.py`) gives async `put`/`get` with a stable hierarchical key layout (`build_r2_key`), plus an existing `artifact_read` tool the model can call. The durable store exists; the insertion-time hook and an *exact-replay* affordance do not.

The gap FRE-475 names is the **tail, at insertion, with a real digest and a durable retrieval path** — a different band, a different time, and a stricter lossiness contract than ADR-0061.

### What the field does about this exact problem

- **Anthropic `clear_tool_uses_20250919` context editing** + the **memory tool** are the closest industrial analogue — built for "agentic workflows with heavy tool use." Knobs: `trigger` (input-token threshold), `keep` (N most-recent tool uses kept verbatim), `clear_at_least` (minimum tokens cleared), `exclude_tools` (per-tool opt-out); a placeholder marker is left in place; the model is prompted to save load-bearing facts to a file-based memory before a clear. **Critically, Anthropic's docs state tool-result clearing *invalidates the cached prefix* at the clear point** — hence `clear_at_least`, to make the forced cache-write worth it.
- **"The Complexity Trap"** (arXiv 2508.21433) measured, on SWE-bench Verified (a tool-heavy code-agent regime close to ours), **deterministic observation masking** (head/tail truncate + placeholder, keep the *action* visible, mask only the *observation*): **54.8 % solve @ $0.61/instance** vs **LLM-summary 53.8 % @ $0.64** vs **raw 53.4 % @ $1.29**. Masking matched-or-beat summarization on cost *and* solve rate. Caveat: scaffold-dependent — a different harness (OpenHands) favored summarization (42 % vs 30 %).
- **ReadAgent** (DeepMind — gist memory + look-up-original-on-demand, ~20× effective context) and **MemGPT** (virtual-context paging) establish the primitive every retrieval-preserving design uses: **compact reference in-context + on-demand re-expand against a durable store**. That is exactly the primitive FRE-465 (ADR-0081 D5) defines for cold *conversation history*.
- **OpenAI** Agents SDK / Responses API: server-side `compaction` at a token threshold, and `truncation: auto` that preserves head+tail of tool output with an omitted-content marker.

Two conclusions fall out, and they shape the decision:

1. **Insertion-time compression is better than after-the-fact clearing on the cache axis — for the dominant case.** When we digest *before the full bytes ever enter the transcript* (case (a), D1 — the bulk of the 768 k), those bytes are never in the cached prefix, so there is **nothing to invalidate**: a pure forward append, consistent with ADR-0081's frozen layout. The narrow exception is a `read` pinned for a pending edit and digested in a later round (case (b), D1) — that *does* invalidate once, exactly like Anthropic `clear_tool_uses`, so we apply the same `clear_at_least`-style economic gate to it. **Both benefits are conditional on byte-stability** (D3): a digest that churns its own bytes on replay re-creates the very problem.
2. **Deterministic beats an LLM summarizer for the synchronous hot loop** — on evidence, not preference — and it avoids injecting a serial LLM call into an already-23-round turn. The caveat (scaffold-dependence; structured outputs whose load-bearing fact is in the *middle*) is handled by making "deterministic" mean **format-aware**, and by leaving an *offline* summarizer path open (D2).

---

## Decision

Compress large tool results to a deterministic, **byte-stable** digest in a **per-round pass over the transcript tail** — digesting most results **before their verbatim bytes ever enter `ctx.messages`** (the dominant *birth-time* regime), while a narrow class (a `read` pinned for a pending edit) enters verbatim and is digested on **deferred release** in a later round — persisting the full bytes to R2 and leaving an imperative, exact-replay affordance in the transcript. ADR-0085 is an **extension of ADR-0081's tiered-context model**, adding a fourth tier:

| Tier | Lives in | Standing cost | Fidelity | Owner |
|------|----------|---------------|----------|-------|
| Hot — salient highlights | volatile tail | tiny | distilled | ADR-0081 D3 |
| Warm — frozen narrative | cached prefix | paid once | lossy | ADR-0081 D2 |
| Cold — full conversation history | Postgres/ES, on demand | ~0 | lossless | ADR-0081 D5 / FRE-465 |
| **Tool-result — insertion-time digest** | **volatile tail (digest) + R2 (full)** | **tiny** | **lossy digest, lossless on re-expand** | **this ADR** |

### D1 — Compress at insertion, as a per-round tail pass

The compression hook runs in `executor.py` **before each `ctx.messages.extend(tool_results)`** (line ~3454) as a **per-round pass over the accreted tail**, not a one-shot transform of a single result at the instant of its birth (Codex round-2 NEW-B2 — the abandonment scan needs subsequent rounds to exist). Each round the pass:

1. **digests newly-arrived oversized results that are not pinned** (D4) — these are digested *at birth*, before their verbatim bytes ever enter `ctx.messages`; and
2. **re-evaluates deferred pins from prior rounds** (D4) and digests any whose release condition is now met.

Consequences:

- For birth-time-digested results (the common case), the digest — not the full bytes — is what is appended to `ctx.messages`, what re-sends each round, and what persists to Postgres session state; a pinned read is the exception (it sits verbatim until release, D4). In steady state the fresh-input tail is bounded at the size of the digests, not the raw output.
- **Two cache regimes, stated honestly.** *(a) Birth-time digestion* (case 1, the dominant cost driver — large `bash`/`read` output not feeding a pending edit): the verbatim bytes **never enter the transcript**, so there is **nothing to invalidate** — pure forward append, the core ADR-0081-consistent win. *(b) Deferred release* (case 2 — a pinned `read` digested in a later round once its dependent edit resolves): the verbatim form *was* in the tail, so replacing it with a digest **is a bounded rewrite that invalidates the prefix from that point once**, exactly like Anthropic `clear_tool_uses`. Case (b) is therefore **economically gated** by `tool_result_digest_min_savings_tokens` (the `clear_at_least` analogue): release-and-digest only when the remaining-rounds savings outweigh the single re-cache; otherwise leave the read verbatim for the rest of the turn. The digest, once written, is frozen (D3), so case (b) invalidates *at most once* per pinned read.
- **The full bytes are written to R2 and the write is *awaited to durable confirmation before the content is replaced by the digest*.** This resolves the durability race (Codex round-1 NEW-1): a result is compressed only once its bytes are durably stored, so the `expand_tool_result` exact-replay contract (D5) is never offered against an unreadable key. Within a batch the puts run concurrently; the digest-substitution waits for them, bounded by **`tool_result_digest_put_timeout_ms`** (Codex round-2 NEW-B1) — **on timeout or put failure the result is left verbatim** (no digest, no broken pointer, no unbounded stall) and the event is logged. The put is off the *generation* path but on the insertion path; the timeout ceiling bounds its latency contribution.
- **Canonical key (single source of truth, used verbatim by D3).** Bytes are stored under `build_tool_result_key(session_id, trace_id, tool_call_id)` → `tool-results/{session_id}/{trace_id}/{tool_call_id}` — a sibling of `build_r2_key` with the same validation discipline, deterministic from three stable IDs, joinable per ADR-0074, minted once. There is exactly one key scheme; D1 and D3 refer to *this* shape.
- This is *distinct from* and *composes with* ADR-0061's middle-band pre-pass: D1 owns the tail at insertion; ADR-0061 still owns whatever survives into the middle band at eviction. A digested message is already small, so the middle-band pass becomes a no-op on it (idempotent).

### D2 — Deterministic, format-aware digests (no LLM in the synchronous hot loop)

The digest is produced by **deterministic, per-tool, format-aware extractors** — never an LLM call on the synchronous insertion path.

- **bash stdout/stderr** — head/tail retention with an elision marker, **plus** always-preserved: exit status, stderr header lines, and any upstream truncation markers. Naive head/tail is *insufficient* for diagnostic output. **⚠️ Amended 2026-06-05 (FRE-486, the park finding): this bullet is the defect. bash stdout is frequently not "diagnostic output" but verbatim *file content* — the model reads source via `cat`/`grep`/`sed` (the always-injected `bash` skill + the `read` tool description actively instruct `grep -n` → ranged read). Head/tail elision deletes the middle of that source (and the middle matches of a `grep` map), corrupting what the model reasons over. The `read`-tool pin (§D4) protects the `read` path but *not* the bash path. No deployable bash-file-content extractor exists that both preserves correctness and saves enough to matter (the harness's read contract already bounds reads), which is why the ADR is parked. Do not enable the flag on file-read-heavy workloads.**
- **read** — outline (structure) + the **matched/ranged regions** the read targeted, dropping bulk. (`read` already encourages grep-then-range, FRE-410, which narrows the blast radius.)
- **Structured formats whose load-bearing fact is in the middle** (diffs/hunks, stack traces, compiler output, test failures) — routed through **format-aware parsers** that retain failing file/line/function frames and nearby hunk context. Head/tail masking on these would preserve the wrong region and let the model act against an absent fact (Codex critique B-HIGH).
- **JSON tool results** — type-specific extraction (key paths, counts, matched values, error fields) rather than ADR-0061's shape-only `keys=…`.
- **Error payloads** — kept **verbatim** (as ADR-0061 already does): the model needs the full failure to recover.
- **Markup / XML / unrecognized formats** — explicitly **out of scope for a structural extractor in this ADR** (Codex round-1 D2). These are rare in this agent's tool surface (bash, read, native REST tools emit text/JSON, not XML documents); they fall back to head/tail with elision + preserved truncation markers. The per-tool-class A/B (Verification §1) is the trigger to add an extractor if any such format shows a material solve-rate loss — building one speculatively now is unjustified scope.

The "no summarizer" decision is **scoped to synchronous insertion only** — it is a hard non-goal for the hot loop. An *offline* LLM summarizer over already-stored R2 bytes is **explicitly out of scope for this ADR** (Codex round-1 over-scope); it is not required for the insertion-time compression story and would be a separate proposal if the per-tool-class A/B (Verification §1) ever shows deterministic extraction losing material solve-rate.

### D3 — Byte-stability is a first-class invariant (the cache-correctness guard)

The D1 cache benefit is regime-dependent (D1): birth-time digestion adds *no* invalidation, deferred release adds *one* bounded, gated invalidation. **Neither regime delivers its benefit unless every digest, once written, is byte-identical on every subsequent resend within the turn and on every cross-turn replay from Postgres** (Codex critique A-HIGH) — i.e. byte-stability governs the prefix *from the digest's write point forward*, not a universal zero-invalidation claim. Therefore:

- Digests are serialized as **canonical JSON with sorted keys**; no field may carry volatile bytes — **no timestamps, no retry counts, no presigned/expiring URLs, no non-deterministic ordering**.
- The R2 reference embedded in the digest is the **canonical `build_tool_result_key` value (D1) + a content hash**, minted once at insertion and never regenerated on replay. There is no presigned/expiring URL in the digest — only the stable key and hash.
- Digested `role="tool"` messages are added to **ADR-0081's existing byte-fixed-point audit**, so send-time transforms (`/no_think` stripping, role-fixing, `_sanitize_tool_pairs`) are verified not to mutate them. A digest that fails the fixed-point check is a release blocker, not a warning.

### D4 — Retention & dependency-pinning policy

Recency alone is too blunt (Codex critique C-MED): in a parallel tool batch (`executor.py:3454` appends several results at once), the load-bearing `read` may not be among the last *N*.

- **Keep the most recent `keep` tool results verbatim** (Anthropic `keep`-style knob) as the baseline recency guard.
- **Pin by dependency:** the most recent `read` of a given file path is **not compressed while a dependent `edit`/`write` against that path is still pending** (the read→edit/write hazard, Codex critique C-HIGH). Pinning is by path + dependency type, not position. A pin is **released** (the read becomes eligible for compression) on the first of: (a) a **successful** `edit`/`write` against that path (the mutation the read was protecting has landed); or (b) `tool_result_digest_pin_ttl_turns` rounds elapse with no `edit`/`write` referencing that path — the **defined "abandoned" condition** (Codex round-1 D4), detected by the per-round tail pass (D1) scanning the rounds since the pin for calls against the path. A **failed** `edit`/`write` does **not** release the pin (Codex round-2 NEW-B3): a failure is precisely when the model is most likely to re-read or retry against the original content, so the verbatim read stays protected until a success or the TTL. The TTL bounds worst-case pinned-tail growth.
- **Imperative digests:** the placeholder is specific and actionable, e.g. *"Full output hidden (N tokens). Call `expand_tool_result("<key>")` to retrieve verbatim before editing against omitted lines."* — because the model will not retrieve a fact it does not realize is missing (ADR-0081 §267).

### D5 — Exact-replay re-expand tool; converge vocabulary with FRE-465, split the contract

A dedicated tool, **`expand_tool_result(key[, offset, limit])`**, fetches the full bytes from R2 for the digest key, **hash-validated** for exact replay, with **ranged** retrieval.

- **Converge the *vocabulary and UX*** with FRE-465's `recall_session_history` — same mental model ("your context is virtual; you can page in what you need"), same telemetry shape — but **keep the contracts separate** (Codex critique D-HIGH): `expand_tool_result` is **exact byte replay** (R2, hash-validated, fast single-object fetch); `recall_session_history` is **lossy ranked search** (Postgres/ES, semantic+keyword, fan-out latency). Different backends, schemas, latency budgets, error handling. Over-abstracting them into one tool would hide store-specific failure modes.
- **Re-expansion must not re-create the spike** it removes (Codex critique E-MED): expansion size is **capped**, ranged expansion is the default for large `read`/`bash` outputs, and re-expansion tokens are measured on a **separate telemetry dimension** (`tool_result_digest_reexpanded`) from compression.

### D6 — Honor the transcript invariants

- **Tool-pair adjacency** (Codex critique E-HIGH): the digest preserves `tool_call_id`, `role`, and adjacency to its assistant `tool_use` exactly; even a maximally-compressed digest is a well-formed `role="tool"` message so `context_window._sanitize_tool_pairs` never orphans it.
- **Reasoning / extended-thinking compatibility** (Codex critique E-HIGH): on reasoning-API paths, items the API requires intact between the last user message and the function-call output are **not** compressed, and thinking/output headroom is reserved in budgeting before the frozen-append invariant is assumed to hold.
- **Reasoning-block boundary (detection is provider-path-specific).** "Items the API requires intact between the last user message and the function-call output" is not a single rule — it differs by provider path (Anthropic reasoning blocks vs OpenAI reasoning items). The implementation pins the boundary **per provider adapter** (`llm_client/`), not in the compressor; the compressor receives an already-computed "do-not-compress floor" index from the adapter. This ADR fixes the *contract* (a floor exists and is honored), not the per-provider rule (Codex round-1 D6).
- **Lost-in-the-middle** (Codex critique E-MED): high-risk / unresolved digests *may* be pinned near the active tail. This reuses ADR-0081 D6's pin mechanism and is an **optional enhancement, not a gate for the first shippable flag** — the cost win (D1) does not depend on it.

### D7 — Configuration & rollout

- Knobs modeled on Anthropic's vocabulary, in `settings` (never hardcoded): `tool_result_digest_threshold_tokens` (compress above this), `tool_result_digest_keep` (recent verbatim count), `tool_result_digest_min_savings_tokens` (the `clear_at_least` analogue — skip compression that wouldn't pay off; Codex critique E-LOW), `tool_result_digest_pin_ttl_turns` (D4 abandonment bound), `tool_result_digest_put_timeout_ms` (D1 insertion-path latency ceiling; timeout → verbatim), `tool_result_digest_exclude_tools`.
- **Feature-flag gated** (`tool_result_compression_enabled`, default off), rolled out only after the before/after A/B clears the gate — the FRE-433/434 *measure → flag → verify → enable* discipline.

---

## Relationship to ADR-0081 and ADR-0061

ADR-0085 is the **intra-turn tool-result tier** of ADR-0081's tiered virtual-context model — it does not supersede or fork ADR-0081; it fills the one band ADR-0081 explicitly left open (the tail, at insertion). It reuses ADR-0081's byte-fixed-point audit (D3), pin mechanism (D6), and the measure-don't-assert methodology. ADR-0061's middle-band `_pre_pass_tool_outputs` continues to own eviction-time compaction of whatever reaches the middle; because D1 digests are already small, the two passes are idempotent and non-conflicting.

---

## Open decisions (data-gated)

1. **Default threshold and `keep`.** Start conservative (e.g. threshold ~1.5–2 k tokens, `keep` = 3, modeled on Anthropic's example of 30 k/3) and tune against the per-round curve — *not* guessed from message size alone (Codex critique E-LOW).
2. **Shared expand vocabulary surface** with FRE-465 (non-gating): confirm the two tools read as one mental model without merging contracts. This is a *consistency check when FRE-465 lands*, **not a dependency of this ADR's rollout** — `expand_tool_result` ships and is flag-enabled independently of D5/FRE-465.
3. **Cross-turn fate of digests.** A digest persisted to Postgres is already compact; does it ever need the ADR-0081 cold tier, or is the R2 key sufficient cross-turn? Likely the latter; confirm under D5.

---

## Consequences

### Positive

- Flattens the per-round fresh-input curve at its largest source (~40 % of input on `a0a07227`); target ≥30 % total fresh-input reduction with no artifact-quality regression.
- Cache-clean by construction for the dominant case: birth-time digestion is forward-append only, with **no prefix invalidation** (unlike Anthropic `clear_tool_uses`); the narrow deferred-release case invalidates at most once and is economically gated — *all* provided D3 byte-stability holds.
- Lossless on demand: full bytes always retrievable from R2 via exact-replay `expand_tool_result`.
- Composes with, rather than duplicates, ADR-0061 and ADR-0081; converges UX with FRE-465 without coupling backends.

### Negative / tradeoffs

- **Correctness surface:** a too-aggressive digest can hide a load-bearing fact. Mitigated by format-aware extraction (D2), dependency-pinning (D4), imperative re-expand affordance (D4/D5), and verbatim errors — but this is the primary risk and the side-by-side eval is the gate.
- **New invariant to maintain:** byte-stability (D3) is subtle and must be enforced by an automated fixed-point test, or the cache benefit silently evaporates.
- **Extra moving parts:** an R2 write per large result — **awaited to durable confirmation before insertion** (D1), so it adds a small, batch-parallelized R2 round-trip to the insertion path (off the *generation* path); plus a new tool surface (`expand_tool_result`). A put failure degrades safely to leaving the result verbatim.
- **Re-expansion can claw back cost** if the model over-expands; bounded by caps + ranged retrieval + separate telemetry (D5).

---

## Verification

The FRE-475 acceptance gate, measured with the FRE-433 reproducible recipe (research doc §9) — before/after per-round token-curve tables, never single anecdotes:

1. **Fresh-input reduction** — re-run an equivalent artifact-build turn; total fresh input drops **≥30 %** vs the `a0a07227` baseline; the per-round `fresh_in` curve flattens. Report the full per-round table (`model_call_completed`, ascending).
2. **No correctness regression** — side-by-side eval of artifact output (the `feedback_always_include_references` + side-by-side-eval discipline): artifact correctness and completeness unchanged.
3. **Byte-stability fixed point (D3)** — automated test: a digested `role="tool"` message is byte-identical across in-turn resends and a cross-turn Postgres replay; fold into ADR-0081's audit. Release-blocking.
4. **Transcript invariants (D6)** — assert **immediate `tool_use`→`tool_result` adjacency** for every digested message (the digest sits in the exact transcript position its verbatim form would, same `tool_call_id`, no message reordering or splitting) — *not merely* `_sanitize_tool_pairs` orphan-absence, which is necessary but does not prove adjacency (Codex round-1 NEW-2). Plus: with extended-thinking enabled, the do-not-compress floor (D6) is honored and reasoning-block sequencing is valid for each provider path under test.
5. **Re-expand path (D5)** — a turn that digests then needs the verbatim content triggers `expand_tool_result`, retrieves hash-validated exact bytes, and completes correctly; re-expansion tokens tracked separately and do not erase the §1 gain.
6. **Insertion-path latency (D1 NEW-B1)** — the per-round digest pass (incl. awaited R2 puts) adds bounded latency: measure added insertion-path wall-time per round; assert it stays near a single `tool_result_digest_put_timeout_ms` ceiling (puts run concurrently, so the bound is ~one timeout plus coordination overhead, not timeout × N), and that timeouts degrade to verbatim without stalling the next model round.
7. **Backend-aware truth source** — per FRE-433, cache reuse read from the backend's own counters (local `timings.cache_n`, cloud `cache_read_input_tokens`), not a single conflated ES field.
8. `make test` / `make mypy` / `make ruff-check` / `make ruff-format` clean.

---

## References

- **Implements:** [FRE-475](https://linear.app/frenchforest/issue/FRE-475) · research doc `docs/research/2026-06-04-artifact-turn-cost-latency-forensics.md` (trace `a0a07227`)
- **Sibling levers:** FRE-476 (decomposition + unpin complexity), FRE-477 (discovery batching), FRE-478 (artifact output-cap)
- **Internal:** ADR-0081 (cache-aware layout & compaction; D2/D3 live, D5/FRE-465 pending) · ADR-0069 (R2 artifact substrate) · ADR-0061 (within-session compression) · ADR-0074 (identity / joinability) · FRE-468/FRE-473 (cache_control ≤4 clamp) · FRE-410 (ranged read) · FRE-433/FRE-434 (methodology + cross-turn machinery)
- **Industrial:** Anthropic — [Context editing](https://platform.claude.com/docs/en/build-with-claude/context-editing) (`clear_tool_uses_20250919`), [Memory tool](https://docs.claude.com/en/docs/agents-and-tools/tool-use/memory-tool), [context-engineering cookbook](https://platform.claude.com/cookbook/tool-use-context-engineering-context-engineering-tools) · OpenAI — [Agents SDK context management](https://openai.github.io/openai-agents-python/context/), [Responses API conversation state](https://developers.openai.com/api/docs/guides/conversation-state) · Google — [Gemini context caching](https://ai.google.dev/gemini-api/docs/caching)
- **Academic:** "The Complexity Trap: Simple Observation Masking Is as Efficient as LLM Summarization for Agent Context Management" ([arXiv 2508.21433](https://arxiv.org/html/2508.21433v1)) · ACON ([arXiv 2510.00615](https://arxiv.org/pdf/2510.00615)) · Active Context Compression ([arXiv 2601.07190](https://arxiv.org/pdf/2601.07190)) · ReadAgent — gist memory ([DeepMind](https://deepmind.google/research/publications/74917/)) · MemGPT (Packer et al., arXiv 2310.08560) · "Lost in the Middle" (Liu et al., arXiv 2307.03172)
