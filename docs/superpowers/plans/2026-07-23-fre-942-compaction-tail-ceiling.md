# FRE-942 — Compaction cannot shrink the case it fires for: decision + fix

**Ticket:** FRE-942 (Approved, Tier-1:Opus, stream:build2)
**Backing ADRs:** ADR-0061 (within-session compression) · ADR-0081 §D3 (cache-aware frozen reset) ·
ADR-0085 / FRE-486 (intra-turn tool-result digest, parked) · ADR-0092 (compaction observability)
**Prior evidence:** `docs/research/2026-07-17-fre-908-compression-gate-proof.md` ·
`tests/test_orchestrator/test_compression_gate_proof.py`
**Revision:** v3 — v1 and v2 were rejected by codex plan-review. Changes recorded in §6 / §7.

---

## 1. The decision (AC-1)

**Fix compaction where it breaks.** Reject the ADR-0085 digest. The fix is *not* the "exempt an
oversized trailing message" variant the ticket proposed — that variant is unimplementable as stated
(see §6/F2). The correct fix is to make the tail band a **bounded, contiguous, user-aligned suffix**,
which removes an entire class of defects at once and is a net *simplification* of `_extract_tail`.

### Evidence — production, not synthetic

289 real within-session compactions (`within_session_compression_completed`, `agent-logs-*`, all
2026-05; 258 `hard` + 31 `soft`):

| Measure | Value |
|---|---|
| Records with **zero or negative** net middle reduction | **128 / 289 (44%)** |
| Records whose tail exceeded its own 24,000-token floor | 11 / 289 (4%) |
| Records still ≥ the 81,600 hard threshold **after** compaction | 2 |
| **Worst case post-compaction working set** | **254,484 tokens — 2.65× the 96,000-token window** |
| Per-record middle saved: min / median / max | −128 / 162 / 33,640 |

Worst record (2026-05-08T06:46:08, `trigger=hard`): 82 messages → 7, middle 26,883 → 377,
`tail_tokens = 254,071`. Compaction ran, recorded success, left the working set at 2.65× the window.
Two seconds later a `soft` pass fired on the same session with `middle_tokens_in = 0` — nothing left to
compress, tail unchanged.

> **Evidence honesty (codex F1).** The log record carries band totals and message counts, not the
> per-message breakdown. It therefore proves *the tail band reached 254,071 tokens*; it does **not**
> discriminate "four ~63k messages" from "one ~250k message plus three small ones forced by
> `min_turns`". Both shapes are produced by the same defect (no ceiling) and both are fixed by the
> design below, so the decision does not rest on that distinction — but the plan must not claim it.
> Wording corrected from v1.

### Mechanism — three defects in one function

`_extract_tail` (`within_session_compression.py:86-153`):

1. **No ceiling.** The walk (`:124-128`) breaks only when **both** floors hold
   (`min_tokens=24,000`, `min_turns=4`), with no upper bound on any single message or on the total.
   `min_turns` keeps pulling messages in *after* `min_tokens` is already satisfied — pinned by the
   existing test at `test_within_session_compression.py:135`. `grep -rn "max_tail\|tail_max" src/`
   → no hits: no ceiling knob exists anywhere.
2. **Non-contiguous result.** The tool-pair repair (`:130-151`) adds an arbitrarily-distant earlier
   assistant index into a set. Both callers then derive the middle boundary as
   `len(messages) - len(tail)` (`:288`, `:418`), which is only valid for a contiguous suffix.
   **Reproduced** at the `_extract_tail` level: a message appears in *both* the middle and the tail.
3. **The repair is undone by the alignment that runs after it.** `_tail_starting_on_user` (`:215`)
   is applied *after* the repair by both callers (`:287`, `:417`) and drops the leading non-user
   messages — including the assistant the repair just pulled in. **Reproduced:** a tail whose `tool`
   message has no backing assistant (`orphaned tool_call_ids in tail: {'c1'}`), which
   `_sanitize_tool_pairs` then silently deletes downstream — losing the tool result the repair existed
   to protect.

Defect 2 is closed *by accident* in the current callers (alignment only ever drops leading non-user
messages, so the aligned tail is contiguous); defect 3 is live. Both are pre-existing, both live in the
function this ticket must change, and the fix below closes all three by construction — so they are
folded into this PR rather than ticketed separately (build SKILL §5).

### Why not the ticket's option 2 (enable the ADR-0085 digest) — rejected

1. **The FRE-486 objection stands unchanged.** `_headtail` deletes the middle of unstructured streams;
   bash-routed `grep -n` maps and `cat`s are the load-bearing artifacts of the in-prompt read contract
   (`docs/research/2026-06-05-tool-result-compression-park-decision.md:57`), and the one live flag-on
   comparison failed (ibid.:46).
2. **It would not have prevented the observed failures**, which are band-level, not per-result.
3. **Its target scenario is not reachable on today's tool surface.** Census over 6,041 captured tool
   results: 48 ever exceeded the 24,000-token tail floor, **46 of them from `query_elasticsearch`,
   which no longer exists in the tool registry**; last occurrence 2026-05-28. Since 2026-06-01, across
   907 results and 20 tools, the largest single result is **19,848 tokens** (`bash`, hard-capped at
   51,200 bytes) — below the floor; zero exceeded it.

Per codex F3, this census is **externally sourced**, so it is made reproducible: the query is checked in
as `scripts/analysis/fre942_compaction_census.py` (§2 Step 0). It is also **not** treated as a forward
guarantee — tool surfaces change, which is exactly why the fix is a structural bound rather than a
per-tool cap.

### Why not "neither"

Defensible before this measurement, not after. 44% zero-reduction and a 2.65×-window post-compaction
working set are recorded production outcomes of code that is unchanged since. Exposure is *currently*
low because traffic is (317 tool results in July vs 3,502 in May), not because anything was fixed.

### Live headroom (AC-1's "live numbers")

FRE-944/945 restored the gateway-path emits. Since: `cache_reset_decision` — 6 emits, one session,
`accumulated_tokens` 26 → 639 against `accum_max_tokens = 48,000`;
`conversation_context_loaded.estimated_tokens` p50 407, max 639. Today's sessions sit ~2 orders of
magnitude below every threshold. This confirms "latent, not live" for *current* traffic, and is why the
response is a small bounded fix, not a new mechanism.

**Out of scope, flagged:** the frozen-reset *action* (`_maybe_frozen_reset`, `executor.py:3187`) remains
structurally unreachable on gateway turns — it sits below the gateway branch's terminal `return`
(`executor.py:3160`); `frozen_reset_fired` is 0 all-time. That is ADR-0092 open item #7.

---

## 2. Implementation

### Step 0 — check in the evidence (codex F3/F6)
`scripts/analysis/fre942_compaction_census.py` — reproduces both censuses (the 289-record compaction
outcome table and the tool-result size distribution) from `agent-logs-*` / `agent-captains-captures-*`.
Read-only ES queries, no writes, no LLM.

→ **verify:** `uv run python scripts/analysis/fre942_compaction_census.py` reprints the §1 tables.

### Step 1 — failing tests first (TDD)
Extend `tests/test_orchestrator/test_compression_gate_proof.py` (AC-2: extend the existing harness).
Two new cases plus one existing-test inversion:

- **(a) accumulation shape** — four trailing ~50k-token tool results. Assert the contract in Step 3.
- **(b) single-spike shape** — reuse the existing FRE-908 fixture.
- **(c) invert `test_compress_in_place_cannot_shrink_a_tail_resident_spike`** (`:127`). Its assertions
  (`tokens_saved == 0`, `middle_tokens_out == middle_tokens_in`) become **false** under the fix: the
  spike no longer lands in the tail, so the pre-pass reaches it. Rename + flip, exactly as FRE-910 did
  to the AC-4 test in this same file, and cite FRE-942 in the docstring. **This is the AC-2 proof** —
  the checked-in test that documented the defect now documents the repair.

Plus, in `tests/test_orchestrator/test_within_session_compression.py`, unit tests for every numbered
clause of the Step-3 contract: contiguity, `[]` on no-user-turn, ceiling, single-message exemption,
**alignment defeating the floors** (codex R2 blocking #3), the three degenerate inputs, and pair safety
asserted **by order** — the backing assistant precedes its tool message *in the returned list* — not by
global ID presence (codex R2).

**`build_frozen_reset` sanitiser-fixed-point probe (codex R2 blocking #1/#2).** ADR-0081:115 requires
the persisted reset output to either be the post-sanitiser wire form *or* be provably a sanitiser
no-op. `build_frozen_reset` persists `head + recap + tail` unsanitised
(`within_session_compression.py:303` → `executor.py:1383` → `:4885`). Add a test asserting
`sanitise_messages(result.messages) == result.messages` over two fixtures: (a) well-formed pairs, and
(b) a **reverse orphan** — an assistant carrying two `tool_calls` where one produced no result
(reachable when a call arrives with no tool name, `executor.py:4543`).
**This is a probe, not a presumed fix.** If (a) passes and (b) fails, that is a *pre-existing* defect
(the deleted repair only ever handled forward orphans) and a genuine design decision — whether reset
should sanitise, exclude, or reject such a turn. In that case: do **not** silently bolt
`sanitise_messages` into the reset path (it can truncate history to the last clean user turn, a
behaviour change in a path with no live traffic to validate against). Record the result, keep the
passing half as a regression guard, and file a Needs-Approval ticket. Either outcome is reported in the
handoff; the contract is never *claimed* without the probe's evidence.

→ **verify:** `make test-file FILE=tests/test_orchestrator/test_compression_gate_proof.py` — (a) and
the contract tests FAIL against current code; (c) currently passes and will flip.

### Step 2 — `within_session_max_tail_ratio` setting
`config/settings.py` beside `within_session_min_tail_ratio` (`:972`). Default **0.35**; bounds
`gt=0.0, lt=1.0` (codex F6). Extend the existing tail validator (`:2383-2393`) to require
`max_tail_ratio > min_tail_ratio`.

→ **verify:** `make test-k K=tail_ratio` · `make mypy`.

### Step 3 — rewrite `_extract_tail` as a bounded contiguous suffix
Replace the index-set walk with a start-pointer walk. **Explicit precedence** (codex F7 point 3),
highest first: contiguity → user-alignment → ceiling → floors.

```
1. start = len(messages); used = 0
2. walk backward while start > head_len:
     next_tokens = estimate_message_tokens(messages[start-1])
     if start < len(messages) and used + next_tokens > max_tokens:  break   # ceiling
     start -= 1; used += next_tokens
     if used >= min_tokens and (len(messages) - start) >= min_turns: break  # floors
3. advance start forward to the first index >= start whose role == "user";
   if none, return []            # folds _tail_starting_on_user in
4. return messages[start:]
```

- The ceiling test is skipped for the **first** message (`start == len(messages)`), so the walk never
  returns empty because one message is oversized; alignment in step 3 may still drop it, which is the
  correct outcome for a lone oversized tool result — it falls to the middle where the pre-pass
  replaces it with a descriptor.
- **Delete the tool-pair repair (`:130-151`).** It is unnecessary by construction: a contiguous suffix
  beginning at a `user` message cannot contain a tool message whose assistant lies outside it, because
  an assistant/tool pair never straddles a user turn. `_sanitize_tool_pairs` remains the backstop for
  malformed input. This removes defects 2 and 3.
- **Delete `_tail_starting_on_user`** (`:215`) and its two call sites (`:287`, `:417`) — folded into
  step 3, so alignment can no longer run *after* pair repair.

**Stated contract** (replaces v1's impossible `tail_tokens <= ceiling` — codex F7 point 1;
precedence and degenerate inputs pinned per codex round 2):

> **Precedence, highest first: contiguity → user-alignment → ceiling → floors.**
>
> 1. The tail is always a **contiguous suffix** `messages[start:]` with `start >= head_len`.
> 2. It is `[]` when no `user` turn is available at or after the walk's start.
> 3. Its token sum is `<= max_tokens`, **except** when the tail is a **single message** — exempt so
>    the ceiling can never delete the most recent message outright. (Wording tightened per codex R2:
>    the exemption is one *message*, not one semantic turn; a tool turn may span an assistant plus
>    several tool results, and only the last message is exempt.)
> 4. `min_tokens` / `min_turns` are floors on the **walk**, not on the returned value: forward
>    user-alignment may reduce the result below either, and the ceiling may stop the walk before
>    either is met. Both are best-effort and are outranked by rules 1-3.
> 5. Degenerate inputs: `max_tokens <= 0` behaves as rule 3 (one message retained, then alignment);
>    `min_turns <= 0` still admits at least one candidate before the floors are evaluated;
>    `max_tokens < min_tokens` is accepted — the ceiling simply wins. The settings validator makes
>    all three unreachable from configuration.
> 6. It contains no orphaned tool message, and every kept `tool` message's backing assistant
>    **precedes it in the returned list** (order asserted, not just ID presence — codex R2).

→ **verify:** Step-1 tests pass · `make test-file FILE=tests/test_orchestrator/test_within_session_compression.py`.

### Step 4 — thread the ceiling through both callers
`compress_in_place` (`:350`) and `build_frozen_reset` (`:240`) each gain `max_tail_tokens: int | None`,
defaulting to the setting — mirroring the existing `min_tail_tokens` parameter. Both drop their
`_tail_starting_on_user` call.

→ **verify:** full `make test`.

### Step 5 — documentation
- **ADR-0061 (AC-3): correct, do not retire.** Status is `Accepted — Implemented 2026-05-01 (FRE-251)`,
  false in both directions. Retiring would be wrong — the hard trigger is the only live overflow
  backstop, and this ticket makes it effective. New status records: soft trigger retired (FRE-941),
  hard trigger proved zero-reduction (FRE-908), repaired here (FRE-942).
  **§D3 must be explicitly amended, not merely annotated** (codex F5): ADR-0061:101 mandates walking
  until both floors hold and :113 justifies `min_turns` as *"prevents a single dump from becoming the
  tail"*. Record that the ceiling now outranks `min_turns`, and that D3's stated intent is better
  served by it — a single dump can no longer become the tail at all.
  **State the residual plainly** (codex F5): the ceiling bounds *accumulation*; a single message above
  the ceiling that survives user-alignment is still preserved verbatim, with Stage 7 / `apply_budget`
  as the remaining backstop. Do not claim the hard gate is unconditionally repaired.
- **ADR-0092 (AC-4):** close open item **#6** as **Resolved** — the reconciliation already landed in
  the ADR-0081 direction: §D3 Decision 4 carries an "As-shipped correction (2026-06-16)" documenting the
  shipped `build_frozen_reset` / `frozen_reset_fired` path and the unextended `Literal["soft","hard"]`
  trigger. Item #6 was left open in error. Refresh its drifted refs
  (`within_session_compression.py:237` → `:240`; `executor.py:982` → `:1391`). Note explicitly that
  this closes a *documentation* divergence, not the item-#7 behavioural gap (codex F6).
- **FRE-908 research doc:** addendum to Finding 3 — production confirmation + the fix + the inverted test.

→ **verify:** `pre-commit run --all-files` (ADR index/status hook, FRE-952).

### Step 6 — quality gates
`make test` · `make mypy` · `make ruff-check` · `make ruff-format` · `pre-commit run --all-files` ·
code-review skill at **high**. security-review not indicated (no inputs/subprocess/auth/secrets/network).

---

## 3. Acceptance criteria → proof

| AC | Proof |
|---|---|
| 1 — decision recorded, grounded in live numbers | §1: 289-record production table, 6,041-result size census (reproducible via Step 0), live `cache_reset_decision` headroom. Both ticket options weighed and rejected with evidence. |
| 2 — test reproduces zero-reduction vs current code, proves the fix reduces it | Step 1(c): the checked-in FRE-908 test asserting `tokens_saved == 0` flips to assert real reduction; Step 1(a) adds the accumulation shape. |
| 3 — ADR-0061 status corrected or retired, with evidence | Step 5: corrected (not retired); §D3 amended; residual stated. |
| 4 — ADR-0081/0092 divergence closed; ADR-0092 item resolved | Step 5: closed in the ADR-0081 direction (shipped 2026-06-16); item #6 → Resolved, refs refreshed. |

## 4. Deploy
Gateway rebuild required. **Ask-first.** No user-visible change at current session sizes — the ceiling
binds only above 33,600 tail tokens, which today's traffic never approaches.

## 5. Out of scope (flagged, not built)
- Frozen-reset action unreachable on gateway turns (ADR-0092 item #7).
- `reasoning_content` estimator blind spot (FRE-908 Finding 1 / FRE-755).
- Removing the dormant ADR-0085 digest infra.

## 6. What changed from v1 (codex plan-review, verdict: Reject)
| # | Finding | Resolution |
|---|---|---|
| F1 | "several merely-large messages" unverifiable from the log record | Claim withdrawn; §1 now states only what the record proves |
| F2 | `tail_tokens <= ceiling` impossible under pair-repair + last-message exception | Replaced with the explicit contract in Step 3; repair deleted, so the overshoot source is gone |
| F3 | "always keep the last message" undone by `_tail_starting_on_user` returning `[]` | Accepted: alignment folded into the walk; a lone oversized tool result now *correctly* falls to the middle |
| F4 | non-contiguous tail; middle boundary derived from `len(tail)` | **Verified** (reproduced). Fixed by construction — contiguous suffix |
| F5 | ADR-0061 D3 `min_turns` invariant needs amending, not annotating; "repair" overstated | Step 5 amends D3 explicitly and states the residual |
| F6 | settings bound; census not reproducible; ADR-0092 #6 is doc-only | `lt=1.0` added; census script checked in (Step 0); #6 scope stated |
| F7 | test internally contradictory; degenerate-input tests missing; no post-compaction target | Test (a) rewritten to the new contract; degenerate cases enumerated in Step 1 |
| — | *(not raised by codex)* pair repair re-orphaned by post-hoc alignment | **Verified** (reproduced); fixed by the same restructure |

## 7. Codex round 2 (verdict: Reject — 3 blocking items)
Round 2 **validated the core design**: the user-anchored contiguous suffix holds for every
executor-produced shape it checked (parallel tool calls, enforced-expansion / hybrid / force-synthesis
messages — all explicit `user` turns; volatile inlining preserves role and position), the Step-3 walk
has no off-by-one or termination bug, and it traced the FRE-908 single-spike fixture concretely to
confirm Step 1(c): the spike falls to the middle and `_pre_pass_tool_outputs` reduces it.

| # | Blocking finding | Resolution in v3 |
|---|---|---|
| R2-1 | `build_frozen_reset` may persist reverse orphans, violating the ADR-0081:333 sanitiser-fixed-point contract | Step 1: added as an explicit **probe** with a stated decision rule; pre-existing, not silently "fixed" |
| R2-2 | Pair-safety test covers orphaned results only, not orphaned assistant calls | Step 1: reverse-orphan fixture added; assertions check **order**, not global ID presence |
| R2-3 | Floors can be defeated by forward alignment — undocumented, untested | Step-3 contract clause 4 pins the precedence; a dedicated test added |
| R2-4 | Contract said "single message", rationale said "most recent turn" | Contract clause 3 tightened |
| R2-5 | Degenerate inputs underspecified | Contract clause 5 defines all three |
| R2-6 | Census script does not exist yet | It is Step 0 — written before any src change, so the evidence lands first |

**Not re-run for a round 3.** The remaining items are test-coverage and contract-wording specifics, not
design questions; a third review round would be reviewing prose rather than approach.
