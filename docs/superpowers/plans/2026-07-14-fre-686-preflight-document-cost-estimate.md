# FRE-686 — ADR-0102 T5: Pre-flight cost estimate + threshold confirmation + cloud cost metering (documents)

**Ticket:** FRE-686 · **Backing:** ADR-0102 §7b, ADR-0065, ADR-0099 · **Blocked-by (merged):** FRE-691 (shared cost spine), FRE-684 (routing/turn-assembly), FRE-683 (document resolution)

## Scope

Close the documented interim gap left by FRE-684 (`docs/superpowers/plans/2026-07-13-fre-684-capability-routing-turn-assembly.md` lines 127-136): the pre-flight cloud-attachment cost gate (`_maybe_confirm_attachment_cost`) only ever estimates *image* blocks. A cloud-routed native-PDF or rasterized-PDF turn currently makes **no** disclose-and-confirm prompt before spending — only the generic hard `cap_usd` ceiling bounds it. T5 extends the gate to price document Tier-2 (vision) delivery, reusing the FRE-691 attachment-agnostic estimator (`estimate_attachment_cloud_cost_usd`) exactly as its docstring anticipates ("documents pass a page-multiplied token count").

**Not in scope** (confirmed via code survey, no changes needed):
- `cost_gate/gate.py` (reserve/commit/refund) — fully generic, untouched.
- `llm_client/cost_estimator.py`, `llm_client/pricing.py`, `llm_client/litellm_client.py` reserve→commit wiring — already prices/meters whatever blocks are in `messages` via `litellm.token_counter()` / `litellm.completion_cost()`. AC-10's "committed cost" half is a **test**, not new logic (T2/FRE-682 already put pricing in `models.cloud.yaml`).
- `llm_client/message_content.py::count_content_tokens` (context-window budgeting) — a related but separate blind spot (a multi-page native `document` block is under-counted there too), explicitly out of this ticket's ADR-cited acceptance criteria. Not touched.
- PWA (T8/FRE-687), joinability probe assertions (T6/FRE-688).
- `AttachmentRef.processing_target` / routing precedence — T4's concern, unchanged.

## Root design decision

`ResolvedDocuments` (from `document_resolution.py`) currently exposes `blocks`, `disclosures`, `used_tier2` — no page count. This is enough for **rasterize** delivery (one `image_url` block per page → `len(blocks)` already is the page count, and those blocks are visually/structurally identical to attachment images, so they fold directly into the existing image-cost bucket). It is **not** enough for **native_pdf** delivery: one `document` block can represent up to 100 selected pages, so `len(blocks)` under-counts by up to 100x. The estimator needs the *actual selected page count* for native delivery specifically.

So: add one new field, `native_pdf_page_count: int`, to `ResolvedDocuments`, threaded up from `_resolve_one_document`. Everything else (rasterized doc pages, plain images) reuses the existing `resolved_blocks`-shaped estimate path unchanged.

## Files touched

### 1. `src/personal_agent/llm_client/message_content.py`
Add a new constant next to `IMAGE_BLOCK_TOKEN_ESTIMATE`:
```python
# Per-page token estimate for the native PDF document block (ADR-0102 §4 cost
# note: "per page you pay both ~1.5-3k text tokens and image tokens" — provider
# extracts text AND rasterizes each page). Upper-bound text (3000) + one image
# tile (IMAGE_BLOCK_TOKEN_ESTIMATE, 1600) = 4600. Deliberately upper-bound, not
# midpoint: this gates a user-facing spend-confirmation threshold, so erring
# toward asking for confirmation is the safe direction (ADR-0102 "the user is
# never surprised by an expensive PDF"). Approximate by construction (ADR-0102
# §"Pre-flight estimate is approximate"); reconciled at commit via real usage.
DOCUMENT_NATIVE_PAGE_TOKEN_ESTIMATE = 4600
```
(codex plan-review, 2026-07-14: corrected from an initial midpoint estimate of 3850 to the ADR's upper bound — the confirmation gate should skew toward asking, not under-warning.)

### 2. `src/personal_agent/orchestrator/document_resolution.py`
- `ResolvedDocuments` dataclass: add `native_pdf_page_count: int` field (4th field, after `used_tier2`), with a docstring line explaining it's the total pages delivered via the native-PDF block this turn (0 for text/rasterize-only turns).
- `_resolve_one_document` return tuple grows from 5-tuple to 6-tuple, adding `native_pdf_pages: int`:
  - Tier-1 (text) return: `0`.
  - Budget-exhausted early return: `0`.
  - Native-pdf-built-successfully branch: `len(selected_pages)`.
  - Native-pdf-rejected-for-size branch (`native_block is None`): `0` (nothing was actually sent).
  - Rasterize branch: `0` (rasterized pages are counted via block count, not this field).
- `resolve_documents`: accumulate `native_pdf_page_count` across documents (sum), thread into both `ResolvedDocuments` construction sites (the empty early-return and the final return).

### 3. `src/personal_agent/orchestrator/executor.py`
- `step_init` (~line 2401-2472): initialize `native_pdf_page_count = 0` alongside the existing `document_blocks`/`document_disclosures` initializers; set it from `doc_resolved.native_pdf_page_count` inside the `if any(a.content_type in PDF_CONTENT_TYPES ...)` branch.
- Build a cost-gate-specific block tuple that folds in rasterized document pages (they're `image_url` blocks, cost-shape-identical to attachment images):
  ```python
  cost_gate_blocks = resolved_blocks + tuple(
      b for b in document_blocks if b.get("type") == "image_url"
  )
  ```
- Change the gate call (currently `if resolved_blocks and not await _maybe_confirm_attachment_cost(ctx, resolved_blocks)`) to:
  ```python
  if (cost_gate_blocks or native_pdf_page_count) and not await _maybe_confirm_attachment_cost(
      ctx, cost_gate_blocks, native_pdf_page_count=native_pdf_page_count
  ):
      return TaskState.SYNTHESIS
  ```
- `_maybe_confirm_attachment_cost` (~line 1734-1851):
  - New parameter `native_pdf_page_count: int = 0` (default preserves the existing image-only call shape used by tests that don't pass it).
  - Import `DOCUMENT_NATIVE_PAGE_TOKEN_ESTIMATE` alongside `IMAGE_BLOCK_TOKEN_ESTIMATE`.
  - Estimate becomes the sum of two calls to the *unmodified* `estimate_attachment_cloud_cost_usd`:
    ```python
    estimate = estimate_attachment_cloud_cost_usd(
        block_count=len(resolved_blocks),
        per_block_tokens=IMAGE_BLOCK_TOKEN_ESTIMATE,
        input_price_per_token=Decimal(str(input_price)),
    )
    if native_pdf_page_count:
        estimate += estimate_attachment_cloud_cost_usd(
            block_count=native_pdf_page_count,
            per_block_tokens=DOCUMENT_NATIVE_PAGE_TOKEN_ESTIMATE,
            input_price_per_token=Decimal(str(input_price)),
        )
    ```
  - Build a human-readable description covering both buckets (replaces the hardcoded `f"{len(resolved_blocks)} attachment(s)"` in the `_maybe_pause_for_constraint` context string and the `ctx.final_reply` dead-end message):
    ```python
    parts = []
    if resolved_blocks:
        parts.append(f"{len(resolved_blocks)} attachment(s)")
    if native_pdf_page_count:
        parts.append(f"{native_pdf_page_count} document page(s)")
    description = " and ".join(parts)
    ```
  - Add `native_pdf_page_count=native_pdf_page_count` to the `attachment_cost_gate_decision` log call.
  - No change to `PendingCloudAttachmentConfirmation` — `ctx.attachments` already carries the PDF attachment ref; re-injection re-resolves everything (including `native_pdf_page_count`) fresh from `step_init` on the next turn.

## Test plan (TDD — failing first)

### A. `tests/personal_agent/orchestrator/test_attachment_cost.py` (estimator — likely no change needed, already generic; add one confirmatory test)
- `test_document_native_page_estimate_math()` — `estimate_attachment_cloud_cost_usd(block_count=10, per_block_tokens=DOCUMENT_NATIVE_PAGE_TOKEN_ESTIMATE, price=Decimal("0.000003"))` equals the hand-computed value. Confirms the existing `test_estimator_is_attachment_agnostic` promise actually holds for the real constant T5 introduces.

### B. `tests/personal_agent/orchestrator/test_document_resolution.py`
Extend existing classes (do not rewrite the file):
- `TestTier2DeliverySelection::test_native_pdf_delivery_produces_document_block` — add assertion `result.native_pdf_page_count == <selected page count>`.
- `TestTier2DeliverySelection::test_rasterize_delivery_produces_image_url_blocks` — add assertion `result.native_pdf_page_count == 0`.
- `TestPageBudgetSelection::test_budget_caps_native_subset_page_count` — assert `native_pdf_page_count == budget` (not the full document length).
- `TestNativeOversizeRejectsWholeDocument::test_native_block_over_total_payload_cap_is_rejected_not_shrunk` — assert `native_pdf_page_count == 0` (rejected → nothing counted).
- `TestAC1NativeTextPath` (either test) — assert `native_pdf_page_count == 0` (Tier 1 never touches it).
- **`TestAC12CleanTaskText::test_extracted_sentinel_confined_to_resolved_blocks`** (line ~786) currently asserts `set(vars(result).keys()) <= {"blocks", "disclosures", "used_tier2"}` — an exact field-shape check that **will fail** the moment `native_pdf_page_count` is added unless updated to include it. Caught by codex plan-review (2026-07-14); fix in the same commit as the dataclass change, not as an afterthought.

### C. `tests/personal_agent/orchestrator/test_attachment_cost_gate.py` (mirror the 7 existing image tests for the document dimension)
New tests, following the file's existing `_ctx`/`_cloud_def`/`_patch_routing` fixtures (add a `_patch_document_routing` helper that monkeypatches `executor_mod._effective_attachment_routing_key` directly — simpler than patching two levels of document routing key resolution):
- `test_document_only_over_threshold_stops_with_prompt_and_no_model_call` — `native_pdf_page_count` sized so `page_count * 4600 * price > threshold`; assert `proceed is False`, estimate + "document page(s)" wording in `ctx.final_reply`.
- `test_document_confirm_proceeds` — `proceed_cloud` decision → `True`, `ctx.attachment_cost_confirmed`.
- `test_document_under_threshold_proceeds_without_pausing` — small page count stays under threshold, `_maybe_pause_for_constraint` not awaited.
- `test_combined_image_and_document_estimate_sums` — non-empty `resolved_blocks` **and** `native_pdf_page_count` both passed; assert the estimate equals the sum of the two independent `estimate_attachment_cloud_cost_usd` calls (proves the two buckets combine rather than one silently overwriting the other).
- `test_reservation_covers_document_estimate` — mirrors `test_reservation_covers_image_estimate`: build a message with a `document`-type block, call `estimate_reservation_for_call`, assert it's `> 0`. *(If `litellm.token_counter` turns out not to recognize the `document` block type — verify this empirically first — assert what it actually does and note the finding in the PR; this is pre-existing `cost_estimator.py`/litellm behavior, not something T5 changes, but AC-9(c) needs the actual behavior documented, not assumed.)*

### D. `tests/test_orchestrator/test_executor.py`
- Update all 6 `SimpleNamespace(blocks=..., disclosures=..., used_tier2=...)` mocks of `ResolvedDocuments` (lines ~1323, 1351, 1379, 1414, 1442, 1496 — verified count via `grep -n "used_tier2=" tests/test_orchestrator/test_executor.py`; a 7th match at ~1396 is inside a docstring, not a real mock) to add `native_pdf_page_count=<N>`:
  - Native `document`-type block mocks → non-zero (e.g. `3`).
  - `image_url`/text mocks or empty-blocks mocks → `0`.
- Decouple the FRE-684 turn-assembly tests from live cost-gate behavior by patching `executor_mod._maybe_confirm_attachment_cost` to `AsyncMock(return_value=True)` wherever the test's actual concern is content-assembly/routing, not cost (`test_document_attachment_injects_document_block`, `test_tier2_document_sets_document_effective_model_key`) — matches the existing convention (`test_step_init_short_circuits_to_synthesis` already does this for images) and avoids the test depending on real `config/models.yaml` pricing values holding under threshold.
- **Replace** `test_document_blocks_do_not_reach_maybe_confirm_attachment_cost` (the pre-T5 lock-in test) with `test_native_pdf_document_reaches_maybe_confirm_attachment_cost`: same setup, but assert `mock_confirm.assert_called_once()` with `native_pdf_page_count` matching the mock's value — i.e. invert the assertion to prove the gap is closed.
- Add `test_rasterize_document_pages_fold_into_image_cost_gate` — a rasterize-delivery document turn (image_url blocks, no raw image attachments) now reaches `_maybe_confirm_attachment_cost` with those blocks included in `cost_gate_blocks` (mock `_maybe_confirm_attachment_cost` and assert its `resolved_blocks`-equivalent arg contains the document's image blocks).

## Acceptance-criteria mapping (what proves this ticket done)

- **AC-9(a)** — `test_document_only_over_threshold_stops_with_prompt_and_no_model_call`: no `_maybe_pause_for_constraint` → `keep_local`/timeout path → `proceed is False`, `ctx.final_reply` carries `$` estimate + proceed/keep-local prompt, no model call (nothing beyond the gate runs — `step_init` returns `TaskState.SYNTHESIS`, verified by extending the existing `test_step_init_short_circuits_to_synthesis`-style assertion for a document-only turn).
- **AC-9(b)** — `test_document_confirm_proceeds`: `proceed_cloud` decision → `True`, confirmed. This proves the gate's own decision boundary at the same depth `test_confirm_proceeds` already proves it for images (FRE-691 precedent) — it does not itself re-prove that a confirmed turn reaches `litellm.acompletion`, which is generic call-sequencing already covered by `test_litellm_gate_wiring.py` independent of attachment type. Matching depth, not inventing a deeper document-specific proof that images never got either (raised and resolved in codex plan-review, 2026-07-14).
- **AC-9(c)** — `test_document_under_threshold_proceeds_without_pausing` (silent proceed) + `test_reservation_covers_document_estimate` (reservation recorded before the call, non-zero, and reflects document content per whatever `litellm.token_counter` actually does with a `document` block — documented, not assumed).
- **AC-10 (committed-cost half)** — the generic reserve→commit control flow is *already* proven attachment-agnostically by `tests/personal_agent/llm_client/test_litellm_gate_wiring.py::test_success_path_reserve_then_commit_with_actual_cost` (mocks `litellm.completion_cost` to a fixed value and asserts the `budget_reservations` row lands `committed` at that cost — content-shape-independent, so it already covers "a document turn commits"). What that test does **not** prove — and what AC-10's wording specifically demands — is that the *token basis* is not text-only. Add one new hermetic unit test, `test_actual_cost_for_response_reflects_document_page_tokens` (new, in `tests/personal_agent/llm_client/test_cost_estimator.py`, alongside the existing `estimate_reservation_for_call` tests): register a model's real per-token pricing via `register_model_pricing`, build two fake litellm-shaped `ModelResponse` objects with `usage.prompt_tokens` set to (a) a small text-only baseline and (b) baseline + a page-multiplied token count matching what a scanned PDF's real per-page charging would report, and call the **real** (unmocked) `actual_cost_for_response` on both. Assert both costs are non-zero and (b) > (a) by roughly the expected per-page multiple — proving the commit-side cost function is sensitive to the image/page-token component of `usage`, not just text tokens. This exercises `litellm.completion_cost()`'s real math (only the `usage` numbers are fabricated, standing in for what the provider would report for a real scanned-PDF call) without a live API call; a live confirmation that the *real* Anthropic API actually reports page-inclusive usage for a native PDF block is AC-SEAM's job (master-run, ADR-0102 "Seam owner" section), not this ticket's.

## Risks / open questions (resolved via codex plan-review, 2026-07-14)

1. ~~`DOCUMENT_NATIVE_PAGE_TOKEN_ESTIMATE` midpoint vs upper bound~~ — **resolved**: set to the ADR's upper bound (4600), not the midpoint, since this gates a user-facing confirmation threshold and should skew toward asking.
2. Whether `litellm.token_counter()` meaningfully counts a base64 `document`-type content block at all (unverified) — affects how strictly AC-9(c)'s "reservation ≈ estimate" reads for native-PDF turns specifically. Plan is to test-and-document rather than block on it, since the generic ADR-0065 reservation mechanism is out of this ticket's file-touch scope.
3. Confirm `config/models.yaml`'s `claude_sonnet` pricing (used implicitly by any FRE-684 test I don't explicitly mock) doesn't accidentally push a previously-passing test over the confirmation threshold now that document-derived image blocks reach the gate — mitigated by explicitly mocking `_maybe_confirm_attachment_cost` in those tests (see §D) rather than relying on real config values.

## Codex plan-review (2026-07-14)

Verdict: go-with-changes. Design (native_pdf_page_count field + reusing the unmodified attachment-agnostic estimator twice) accepted; rasterized-doc-pages-fold-into-image-bucket accepted. Four corrections applied above: (1) token constant raised to the ADR's upper bound 4600, not the midpoint 3850; (2) the exact-field-shape test assertion in `test_document_resolution.py` line ~786 was a missed touch point, now listed; (3) mock count corrected from 7 to 6 real `SimpleNamespace` sites; (4) AC-10's commit-side test strengthened from a vague "TBD" placeholder to a concrete comparative token-basis test, and AC-9(b)'s scope note clarified against over-claiming. No correctness bugs found in the proposed executor.py changes.

## Quality gates
`make test-file FILE=tests/personal_agent/orchestrator/test_attachment_cost.py` · `test_attachment_cost_gate.py` · `test_document_resolution.py` → `make test-file FILE=tests/test_orchestrator/test_executor.py` → full `make test` → `make mypy` · `make ruff-check` · `make ruff-format` · `pre-commit run --all-files`.
