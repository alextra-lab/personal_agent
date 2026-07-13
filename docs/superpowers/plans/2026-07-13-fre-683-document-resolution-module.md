# FRE-683 — ADR-0102 T3: Document-resolution module

**Ticket:** [FRE-683](https://linear.app/frenchforest/issue/FRE-683/adr-0102-t3-document-resolution-module-tiered-selector-content-block)
**Backing ADRs:** ADR-0102 (Document Ingestion), ADR-0101 (Agent Vision Ingestion, foundation reused)
**Branch:** `fre-683-doc-capability-cost-matrix`

## Scope boundary (what this ticket does NOT do)

Per the ticket text, **routing/delivery selection is T4** (FRE-684) and **cost estimation / joinability
are T5/T6** (FRE-686/688). This ticket delivers a **self-contained, orchestrator-agnostic module** that:

- classifies a PDF as Tier 1 (text) or Tier 2 (vision) by inspecting it,
- builds the appropriate content block(s) for a **caller-supplied** Tier-2 delivery mode (native PDF
  block vs. rasterized images) — it does **not** decide which mode to use; that precedence logic
  (`supports_pdf_document` vs `supports_vision`, escalation, fail-closed `AttachmentUnsupportedError`)
  is T4's job, wired into `executor.py`'s routing seam,
- enforces all four ADR-0102 §5 guardrail dimensions, fail-closed,
- never touches `ExecutionContext.user_message` (proves AC-12 by construction — the module has no
  access to it).

**No changes to `orchestrator/executor.py` or `orchestrator/attachment_resolution.py`** in this ticket —
those are T4's integration seam. This keeps the module fully unit-testable in isolation, mirroring how
`attachment_resolution.py` (ADR-0101) was itself built and tested before `executor.py` wired it in.

## Files

- **New:** `src/personal_agent/orchestrator/document_resolution.py` — the module (sibling of
  `attachment_resolution.py`, per ADR-0102's Implementation Notes: "sits alongside... the ADR-0101
  attachment-resolution module").
- **New:** `tests/personal_agent/orchestrator/test_document_resolution.py`.
- **Edit:** `src/personal_agent/config/settings.py` — 6 new PDF-specific guardrail settings (§ below).
- **Edit:** `tests/test_config/test_settings.py` — default-value assertions for the new settings
  (mirrors the existing `attachment_max_total_payload_bytes` pattern at line 227).

## 1. New settings (`config/settings.py`, alongside the existing `attachment_*` block at line ~796)

All `Field(..., gt=0, description=...)`, ADR-0099 config-single-source, tunable via `AGENT_` env prefix
without code change (ADR-0102 Risks table: "the floor and caps are config, adjustable without code
change"):

```python
document_text_density_floor_per_page: int = 100
# Per-page character-count floor used to classify Tier 1 vs Tier 2 (ADR-0102 §1).
# Applied in aggregate: a document is Tier 1 when its total extracted character
# count >= floor_per_page * page_count.

document_max_pages_per_turn: int = 40
# Tier-2 page budget (ADR-0102 §4). min()'d against the Anthropic provider hard
# limit (100 pages) at call time — the provider limit is not owner-tunable.

document_page_max_pixels: int = 1568
# Per-page rasterization long-edge pixel cap before encoding (ADR-0102 §5),
# independent of attachment_image_max_pixels (a PDF page is a distinct guardrail
# dimension from an uploaded photo).

document_page_max_bytes: int = 5_242_880  # 5 MiB
# Per-page encoded byte cap, rasterize delivery only (ADR-0102 §5).

document_max_total_payload_bytes: int = 15_728_640  # 15 MiB
# Total per-turn Tier-2 payload cap (rasterized pages or the native PDF block),
# independent of the per-page cap (ADR-0102 §5).

document_max_extracted_text_chars: int = 200_000
# Tier-1 extracted-text cap (ADR-0102 §5) — ~50k tokens. Over-cap text is
# trimmed with disclosure, never sent unbounded.
```

## 2. Module design (`document_resolution.py`)

### 2.1 Public API

```python
@dataclass(frozen=True)
class ResolvedDocuments:
    blocks: tuple[dict[str, Any], ...]
    disclosures: tuple[str, ...]


async def resolve_documents(
    attachments: Sequence[AttachmentRef],
    *,
    tier2_delivery: Literal["native_pdf", "rasterize"],
    trace_id: str | None = None,
    session_id: str | None = None,
    task_id: str | None = None,
) -> ResolvedDocuments:
    ...
```

- Filters `attachments` to `content_type == "application/pdf"`; anything else is silently ignored (not
  this module's concern — `attachment_resolution.py`'s existing unsupported-type rejection still owns
  every other content type; T3 does not duplicate or touch that logic).
- `tier2_delivery` is a **caller-supplied** mode, not computed here — T4 computes it from
  `ModelDefinition.supports_pdf_document` / `supports_vision` and passes it in. If a document
  classifies as Tier 2 and `tier2_delivery` is needed, this function uses exactly the mode given; it
  never falls back or escalates (that precedence + fail-closed behavior is T4's).
- `trace_id`/`session_id`/`task_id` threaded onto `store.get(...)` and every `log.info`/`log.warning`
  call, mirroring `attachment_resolution.py`'s exact pattern (ADR-0074 joinability — T3 threads the
  identity args it already receives; T6/FRE-688 owns the joinability *probe* assertion).
- Raises `AttachmentUnsupportedError` (reused from `personal_agent.exceptions`, not a new exception) when:
  - `get_artifact_store()` returns `None` (R2 unconfigured) — mirrors `attachment_resolution.py`.
  - The fetched bytes fail to open as a PDF (`pdfium.PdfDocument(raw)` raises) — fail closed rather than
    silently skipping a corrupt upload.
  - The opened PDF has **zero pages** — fail closed rather than emit an empty text/document/image block
    (codex plan-review finding: a zero-page PDF must never silently produce an empty block).

### 2.2 Per-document resolution flow

For each PDF attachment, in submitted order:

1. `raw = await store.get(attachment.r2_key, trace_id=..., session_id=..., task_id=...)`.
2. Open `pdfium.PdfDocument(raw)` (`asyncio.to_thread` — pdfium is sync, matches how `pdf_utils` is
   already used/tested; also matches `attachment_resolution.py`'s `asyncio.to_thread` use for Pillow).
3. `page_texts = [extract_page_text(page) for page in pdf]` (T1, `pdf_utils.extract_page_text`).
4. **Tier classification:**
   ```python
   total_chars = sum(len(t.strip()) for t in page_texts)
   floor = settings.document_text_density_floor_per_page * max(len(page_texts), 1)
   tier = "text" if total_chars >= floor else "vision"
   ```
   (Direct generalization of `pdf_utils.is_text_dense`'s per-string floor to a whole document — reuses
   the same semantic without needing a new T1 function.)
5. **Tier 1 (text):**
   - `full_text = "\n\n".join(page_texts)`.
   - If `len(full_text) > settings.document_max_extracted_text_chars`: trim to the cap, append a
     disclosure string (`"Extracted text for '{title}' was trimmed to {cap} characters (guardrail)."`).
   - `blocks.append({"type": "text", "text": full_text})`.
   - Continue to the next attachment — **no page selection, no Tier-2 work for this document.**
6. **Tier 2 (vision):**
   - `scores = compute_page_scores(pdf)` (T1).
   - `outline = get_outline_page_indices(pdf)` (T1).
   - `budget = min(settings.document_max_pages_per_turn, _PROVIDER_MAX_PAGES)` where
     `_PROVIDER_MAX_PAGES = 100` is a module constant (Anthropic's documented hard limit — not owner
     config, an external provider constraint).
   - Outline-aware selection (thin wrapper over T1's `select_pages_by_salience`, does not modify T1):
     ```python
     def _select_pages(scores, budget, outline):
         if not outline:
             return select_pages_by_salience(scores, budget)
         boosted = [
             replace(s, char_count=s.char_count + _OUTLINE_BONUS) if s.page_index in outline else s
             for s in scores
         ]
         return select_pages_by_salience(boosted, budget)
     ```
     `_OUTLINE_BONUS` a module constant large enough to dominate ranking (e.g. `10_000`).
   - **Guard: empty selection.** `scores` is non-empty here (the zero-page case already raised
     `AttachmentUnsupportedError` in step 2), so `select_pages_by_salience(scores, budget)` with
     `budget >= 1` always returns at least one page — `budget` is `gt=0` by the settings field
     constraint and `_PROVIDER_MAX_PAGES = 100`, so `min()` is never `0`. No separate empty-selection
     branch is needed; asserted by a test (`TestPageBudgetSelection`) rather than defensive code for an
     unreachable state (codex plan-review finding, addressed by making the invariant explicit instead of
     adding dead-code error handling for an impossible input).
   - If `len(scores) > budget`: append a minimal disclosure (`"{n} of {m} pages were not included
     (per-turn page budget)."`) — the rich "included pp. 1–23, dropped p. 9; want more?" narrative +
     working continuation is **T7's (FRE-685) job**, not this ticket's AC slice.
   - **Delivery — `tier2_delivery == "native_pdf"`:**
     - `sub_pdf = build_page_subset(raw, selected_pages)` (T1). Note: T1's `build_page_subset` already
       has its own internal fallback — if PDFium's `import_pages` (vector-quality sub-PDF export) fails
       for a given input, it falls back to rasterizing the selected pages and re-encoding them as an
       image-backed PDF (`pdf_utils.py:199-230`). This module does not need to re-implement that
       fallback — it always receives *some* valid PDF bytes back, or `build_page_subset` itself raises.
     - base64-encode; if the encoded size exceeds `document_max_total_payload_bytes` (checked against
       the running per-turn total, same as rasterize path below) → **reject this document's block**
       with disclosure. **v1 limitation, explicitly not retried:** no shrink-and-retry with a smaller
       page budget, and no fallback to the rasterize delivery mode for this one document (that would
       cross back into a routing/delivery decision, which is T4's boundary per §2.1). Covered by
       `TestNativeOversizeRejectsWholeDocument` (§3) so the behavior is asserted, not just documented.
     - `blocks.append({"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": b64}})`.
   - **Delivery — `tier2_delivery == "rasterize"`:** for each selected page index (ascending order):
     - `image = rasterize_page(page, dpi=150)` (T1 default).
     - Downscale below `document_page_max_pixels` if needed (Pillow `thumbnail`, mirrors
       `attachment_resolution._downscale_if_needed`'s approach but takes a `PIL.Image` directly since
       there's no pre-existing content-type-tagged byte string here).
     - PNG-encode, base64; if over `document_page_max_bytes` after downscale → **drop this page** with
       disclosure (per-page granularity, unlike the whole-document reject on the native-block path,
       because rasterize delivery is already page-by-page).
     - Running total against `document_max_total_payload_bytes`: stop (prefix semantics, matching
       `attachment_resolution.py`'s existing payload-cap pattern exactly) with disclosure for the
       remaining dropped pages.
     - `blocks.append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{encoded}"}})`
       per surviving page.

### 2.3 Guardrail-dimension ↔ AC-7 test mapping

| Guardrail dimension (ADR-0102 §5 / AC-7) | Enforced in | Behavior |
|---|---|---|
| per-page raster pixel dimension | rasterize delivery | downscale (Pillow), never rejected outright |
| per-page image byte size | rasterize delivery | downscale, then drop-with-disclosure if still over |
| total per-turn payload | both deliveries | native: whole-doc reject; rasterize: stop-as-prefix |
| extracted-text size | Tier 1 only | trim-with-disclosure |

Each gets its own parametrized test, per AC-7's explicit language. **Page-count (the budget in §4) is a
separate guardrail dimension from these four** — ADR-0102 §5 lists it alongside the four AC-7 caps, but
AC-7's own test description names only these four; page-count budget enforcement is proven by
`TestPageBudgetSelection` (§3), not folded into the AC-7 parametrized suite (codex plan-review finding —
made explicit so the two test groups aren't conflated).

## 3. Test plan (`test_document_resolution.py`)

Mirrors `test_attachment_resolution.py`'s structure (`_make_attachment` helper, `_mock_store` helper,
patch `document_resolution.get_artifact_store`) plus real (small, synthetic, in-memory) PDFs built with
`pypdfium2`/`Pillow` directly in the test (matches `test_pdf_utils.py`'s existing synthetic-PDF pattern
— reuse/import its fixtures if convenient, don't duplicate).

- `TestAC1NativeTextPath` — a synthetic PDF with real embedded text (no images) → asserts the result is
  exactly one `{"type": "text", ...}` block, no `image_url`/`document` block, and the text contains the
  sentinel string embedded in the source PDF.
- `TestAC2ScannedVisionPath` — a synthetic PDF built with **no text layer** (image-only page(s)) →
  asserts a `document` or `image_url` block (parametrize both `tier2_delivery` values), and **no**
  `text` block is emitted.
- `TestClassificationBoundary` — a synthetic document whose total extracted characters land exactly at
  `floor_per_page * page_count` (boundary, Tier 1) and one character below it (Tier 2) → asserts the
  `>=` boundary is inclusive as coded, locking in the exact threshold behavior (codex plan-review
  finding: lock in the chosen heuristic with an explicit boundary test).
- `TestClassificationMixedDocument` — a document with one dense text page and several near-blank pages
  → asserts the aggregate-floor heuristic's actual behavior on a mixed document (documents, does not
  "fix", the accepted ADR-0102 heuristic risk — codex plan-review finding).
- `TestTier2DeliverySelection` — same scanned PDF, `tier2_delivery="native_pdf"` vs `"rasterize"` →
  asserts the correct block `type` for each.
- `TestPageBudgetSelection` — a multi-page scanned PDF exceeding `document_max_pages_per_turn` →
  asserts `len(blocks) <= budget` (rasterize) or the sub-PDF page count `<= budget` (native), and a
  budget disclosure is present; reuses the AC-5-style blank-vs-dense synthetic pages from
  `test_pdf_utils.py`'s pattern to prove the salience-based (not naive first-N) selection survives
  end-to-end through this module (T1's own AC-5 test already proves the scorer in isolation; this test
  proves the module actually calls it rather than bypassing it).
- `TestOutlineBoost` — a page in the PDF outline that would otherwise lose to a denser non-outline page
  at a tight budget → asserts the outline page is selected.
- `TestAC7PixelCapGuardrail` — rasterize delivery, a page whose render exceeds `document_page_max_pixels`
  → asserts the resulting block's decoded image is within the cap.
- `TestAC7ByteCapGuardrail` — rasterize delivery, monkeypatch the cap very low → asserts the page is
  dropped with a disclosure and absent from `blocks`.
- `TestAC7TotalPayloadGuardrail` — multiple pages/documents, monkeypatch total cap low → asserts a
  strict-prefix drop with disclosure (rasterize) and a whole-document reject with disclosure (native).
- `TestNativeOversizeRejectsWholeDocument` — native-block delivery, monkeypatch the total-payload cap
  low enough that even the budget-selected sub-PDF doesn't fit → asserts the document produces **no**
  block, a disclosure is present, and no shrink-and-retry or rasterize-fallback occurs (proves the
  documented v1 limitation, not just describes it — codex plan-review finding).
- `TestZeroPagePdfFailsClosed` — a syntactically valid PDF with zero pages → `AttachmentUnsupportedError`,
  no block emitted (codex plan-review finding).
- `TestAC7ExtractedTextGuardrail` — Tier-1 PDF whose text exceeds `document_max_extracted_text_chars` →
  asserts the block's `text` is trimmed to the cap and a disclosure is present.
- `TestAC12CleanTaskText` — structural proof: `resolve_documents`'s signature takes no
  `ExecutionContext`/`user_message` argument and `ResolvedDocuments` carries no such field; a resolution
  with a text-layer sentinel unique to the PDF's content proves that sentinel appears **only** inside
  `resolved.blocks`, confirming the module has no channel back into task/user-message text. (Full
  end-to-end proof that `ctx.user_message` stays untouched happens at T4's integration + the ADR-0102
  AC-SEAM live smoke — this ticket proves its own module boundary can't leak.)
- `TestCredentialedFetch` — asserts `store.get` called with `(r2_key, trace_id=..., session_id=...,
  task_id=...)`, mirroring `test_attachment_resolution.py`'s `test_store_get_called_with_r2_key_and_trace_id`.
- `TestCorruptPdfFailsClosed` — invalid PDF bytes → `AttachmentUnsupportedError`, not a silent empty
  result.
- `TestStoreUnconfigured` — `get_artifact_store()` returns `None` → `AttachmentUnsupportedError`.
- `TestNonPdfAttachmentsIgnored` — a non-PDF attachment in the list → no blocks, no error (that
  content type is `attachment_resolution.py`'s problem, not this module's).

## 4. Design decisions flagged for codex plan-review

1. **Tier classification: aggregate floor scaled by page count** (`floor_per_page * page_count`) rather
   than a majority-of-pages vote or a flat single-page floor. Chosen for simplicity and because it's a
   direct generalization of T1's existing `is_text_dense(text, floor)` semantic. Risk: a long document
   with one dense page and many blank ones could misclassify as Tier 1 in edge cases — accepted per the
   ADR's own "mis-classified text layer" risk row (Low-Medium, mitigated by config-tunable floor).
2. **`tier2_delivery` as a caller-supplied literal, not computed here.** Keeps this module fully
   decoupled from `ModelDefinition`/capability config, matching the ticket's explicit "routing/delivery
   selection itself is T4" boundary.
3. **Per-page drop (rasterize) vs whole-document reject (native)** on the total-payload guardrail —
   asymmetric because rasterize is inherently page-granular (like the existing image resolver) while a
   native PDF block is one atomic document (no partial-native-block concept without re-running page
   selection, deferred as a v1 simplification).
4. **No new T1 (`pdf_utils.py`) changes** — outline-boost is a thin wrapper in this module using
   `dataclasses.replace`, not a change to `select_pages_by_salience` itself.

## 5. Quality gates

- `make test-file FILE=tests/personal_agent/documents/test_pdf_utils.py` (regression, unchanged) then
  `make test-file FILE=tests/personal_agent/orchestrator/test_document_resolution.py` then
  `make test-file FILE=tests/test_config/test_settings.py` then full `make test`.
- `make mypy`, `make ruff-check`, `make ruff-format`, `pre-commit run --all-files`.
- Self-review: `code-review` skill at **high** effort (new `src/` module, guardrail/security-adjacent —
  parses untrusted uploaded PDF bytes). `security-review` skill (file/input parsing).

## 6. Acceptance-criteria proof (for the Step-9 ticket comment)

- **AC-1** — `TestAC1NativeTextPath`.
- **AC-2** — `TestAC2ScannedVisionPath`.
- **AC-7** — `TestAC7PixelCapGuardrail`, `TestAC7ByteCapGuardrail`, `TestAC7TotalPayloadGuardrail`,
  `TestAC7ExtractedTextGuardrail`.
- **AC-12** — `TestAC12CleanTaskText` (module-boundary proof; full end-to-end proof deferred to T4 +
  AC-SEAM per the ADR's own seam-ownership model).
