# ADR-0102: Document Ingestion (PDF) — Tiered, Capability-Routed Strategy

**Status:** Proposed
**Date:** 2026-06-29
**Deciders:** lextra (owner), Seshat architecture
**Tags:** uploads, documents, pdf, vision, text-extraction, model-routing, orchestrator, r2, licensing

---

## Context

**What is the issue we're addressing?**

ADR-0101 (Proposed) **decided** the *image* half of attachment ingestion: an uploaded raster image is
resolved into a typed content block at turn assembly, fetched server-side over the credentialed R2
path, and routed to a vision-capable model with fail-closed capability checks. It deliberately **carved
documents (PDF) out** — an image is one bounded visual block (no pagination, no chunking, no OCR-vs-vision decision,
predictable per-image cost), whereas a PDF is categorically different and carries a real decision tree
the image ADR should not bury. This ADR owns that decision tree.

A PDF differs from an image along axes that force genuine design choices:

- **Variable page count with per-page cost that scales** — a long PDF cannot be sent unbounded; it
  needs a page budget and a way to choose *which* pages.
- **Scanned vs native text layer** — a born-digital PDF carries a selectable text layer that can be
  extracted *losslessly and cheaply* (no vision needed at all); a scanned PDF is image-only and
  *requires* OCR or vision to read.
- **A strategy choice with materially different cost/fidelity** — (a) server-side text extraction, (b)
  a provider-side native PDF document block (e.g. Anthropic, which rasterizes + OCRs + applies vision
  per page internally), or (c) rasterize-to-images locally (portable to any vision model, including the
  local primary).

**What is reused, not re-decided (the ADR-0101 foundation):** the structured `AttachmentRef` carrier
threaded through `handle_user_request` → `ExecutionContext`; the credentialed `store.get(r2_key)`
byte fetch that bypasses Cloudflare Access; the `Message.content` widening to a block list; the
turn-assembly injection seam; and capability-driven, fail-closed routing keyed off declared config
flags (never hardcoded model names). ADR-0102 adds **only** the document-strategy layer and one
document-specific capability flag. Its *implementation* therefore depends on the ADR-0101 chain
landing first; this design pass does not.

**What needs to be decided:**

- For a native-text PDF, do we extract text or send it to vision?
- For a scanned PDF, which vision delivery — provider-side native PDF block or our own rasterized image
  blocks — and how does that differ by profile (cloud vs local)?
- What library does text extraction *and* rasterization without a copyleft license? (PyMuPDF/`fitz`,
  the obvious mature choice, is **AGPL-3.0** and disqualified for this project.)
- How is the page budget enforced, and what happens when a PDF exceeds it?

**Grounding facts (verified against config, code, vendor docs, and a cited research pass — 2026-06-29):**

- The upload allowlist already accepts `application/pdf` (`service/uploads_router.py:53,68`); the bytes
  land in R2 exactly as images do, and the metadata canon is Postgres.
- No PDF or imaging dependency exists in the repo today (`pyproject.toml` / `uv.lock`): no PyMuPDF, no
  Pillow, no poppler. The document path introduces its first PDF dependency — license choice is live.
- **`pypdfium2`** (Python binding to Google's **PDFium**) is **Apache-2.0 / BSD-3-Clause** (PDFium
  itself is BSD-style) and does **both** required jobs in one self-contained wheel: text extraction
  *and* page rasterization (`page.render()`), with no system binary and no model downloads. It also
  supports **building a new PDF from a selected subset of pages** (PDFium `FPDF_ImportPages`, exposed
  as `PdfDocument.import_pages`) — needed to honor provider page limits on the native-block path (§4).
  It is "one of the rare Python libraries capable of PDF rendering while not being covered by
  strong-copyleft licenses." **Pillow** (HPND, permissive) handles downscaling/encoding the rendered
  bitmaps. The exact licenses are verified at build by AC-3, not merely asserted here.
- **PyMuPDF is AGPL-3.0** (strong copyleft) — disqualified. **pdf2image** is an MIT wrapper but shells
  out to **Poppler** (GPL); usable as mere aggregation via `subprocess`, but it carries a GPL-binary
  redistribution burden in the Docker image and is unnecessary once `pypdfium2` covers rasterization.
- **Anthropic native PDF support** (official docs): each page is converted to an image *and* its text
  is extracted, both handed to the model — so charts/tables/layout are read, not just text. All active
  models support it. Limits: **32 MB** total request, **100 pages** for 200k-context models (600
  hard max). Cost is standard token pricing with **no PDF surcharge**, but per page you pay **both**
  ~1.5–3k text tokens **and** image tokens — Bedrock's documented datapoint is ~7× the token cost of
  text-only extraction for the same PDF. Sources: URL, base64, or Files API; prompt-cacheable.
- **Frontier products converged on hybrid**, not one-or-the-other: Gemini/OpenAI/Claude all combine
  text extraction with vision; for text-heavy PDFs vision and extraction score *similarly on accuracy*
  while extraction is markedly cheaper. The 2024–26 vision-retrieval shift (ColPali, DSE,
  "screenshot-the-page" embeddings) is real but **scoped to retrieval/indexing over a corpus** — it
  answers "which page do I retrieve," a non-problem when the user handed us the whole PDF in-context.
- `ModelDefinition` (`llm_client/models.py`) gains `supports_vision` from ADR-0101 (rasterized image
  blocks). It has **no** flag for the provider-side native PDF document block — this ADR adds one.
- **Cloud vs local is the ExecutionProfile (ADR-0044).** A conversation is bound to a profile at
  creation; an async context var (`config/profile.py:_current_profile`) carries it through the call
  chain. There is **no per-attachment path control today** — this ADR adds one (owner-requested).
- **Cost is gated and reconciled (ADR-0065).** The cost gate reserves against `cap_usd` caps and
  commits actual cost via `litellm.completion_cost()` (`cost_gate/gate.py`). Cloud Claude pricing lives
  in the cost matrix (`config/models.cloud.yaml`); a document turn needs a **pre-flight estimate** to
  reserve *before* spending — a blank reservation would let an expensive PDF through. The Claude PDF
  path is genuinely expensive (≈7× text), so this is load-bearing, not cosmetic.
- **Joinability is an established pattern (ADR-0074).** `artifact_store.get` already threads `trace_id`
  (`storage/artifact_store.py:222`), R2 keys embed `session_id`, and `TraceContext` carries
  `trace_id`+`session_id`. The document path must thread `trace_id`/`session_id`/`task_id` onto its
  resolution, cost, and routing events so they join back to the turn with no orphans.
- **This control set was missing from ADR-0101 (images) too** — owner-flagged. ADR-0101's
  implementation tickets are still Needs-Approval, so the same per-attachment override, cost metering,
  and joinability threading should be folded into them; this ADR establishes the shared design.

---

## Decision

Ingest PDFs via a **tiered, capability-routed strategy selector resolved at turn assembly**, built on
the ADR-0101 foundation and adding only the document-strategy layer. Concretely:

### 1. Tiered strategy selector (text-first, vision-on-demand)

For an `application/pdf` attachment, resolution chooses a strategy by inspecting the document, not by a
fixed rule:

- **Tier 1 — server-side text extraction (default).** Extract the text layer with `pypdfium2` and
  inject it as a **text block**. Cheapest, lossless for prose, searchable, and works on **any** model
  (no vision capability required). This is the dominant case (reports, papers, exports). Selected when
  the document has a usable text layer — **text density at or above a configured per-page/aggregate
  floor**.
- **Tier 2 — vision (scanned / low-text PDFs).** When text density is below the floor (scanned or
  image-only PDF), the document needs visual reading. Delivery is **profile-/capability-routed** (§3).

Detection is the extraction attempt itself: extract, measure characters per page; below the floor → the
page/document is treated as scanned → Tier 2.

### 2. License-clean dependency stack

PDF handling uses **`pypdfium2`** (Apache-2.0/BSD-3) for **both** text extraction and rasterization,
plus **`Pillow`** (HPND) for downscale/encode. **PyMuPDF (AGPL) is explicitly rejected; pdf2image /
Poppler (GPL) is explicitly rejected.** No copyleft and no GPL system binary enters the build.

### 3. Vision delivery is capability-routed (and adds one flag)

Add **`supports_pdf_document: bool`** to `ModelDefinition` (the model accepts a *provider-side native
PDF document block*) — set true for `claude_sonnet` / `claude_haiku`, false for the local SLM models.
`supports_vision` (from ADR-0101) continues to gate rasterized image blocks. Tier-2 delivery resolves
by declared capability, in precedence order:

- **Model supports the native PDF document block** (`supports_pdf_document: true`, the cloud Claude
  case) → send the selected pages as a **native PDF document block** — highest fidelity, least code,
  provider handles text+image per page, prompt-cacheable.
- **Else model is vision-capable** (`supports_vision: true`, the local SLM case) → **rasterize the
  selected pages with `pypdfium2`** and send **image blocks**, reusing ADR-0101's exact image path.
- **Else** — if the active profile permits cloud escalation (`cloud.yaml`) → escalate **only to an
  escalation model that itself declares `supports_pdf_document` or `supports_vision`**; if the
  configured escalation model is not capable, fail closed (below) rather than escalate to an incapable
  model. If the profile **forbids** escalation (`local.yaml`: `allow_cloud_escalation: false`) → **fail
  fast with a user-visible `AttachmentUnsupported` error** naming the modality. **No silent text-only
  downgrade, no cross-boundary escalation.** An attached document follows the same
  trust boundary the turn's text already crosses — vision introduces no new data-egress boundary.

Because Tier 1 handles native-text PDFs, the expensive Tier-2 paths (native block ≈ 7× cost, or
rasterization) fire **only** for scanned/low-text PDFs — cost-optimal by construction.

### 4. Page budget + content-aware selection (no silent truncation)

A per-turn page budget bounds Tier-2 cost: `budget = min(configured_page_cap, provider_hard_limit)`
(provider limit ≈ Anthropic's 100 pages / 32 MB for the native block). When a PDF's selectable pages
exceed the budget:

- **Skip low-information pages first** using cheap, model-free signals from `pypdfium2`: text length
  per page and ink/image coverage ratio (drop near-blank / pure-whitespace / cover pages); prefer pages
  the PDF outline/TOC marks as substantive.
- **Auto-select the most informative pages within budget** and process the turn — never reject outright,
  never silently truncate.
- **Disclose in the response** which page ranges were included and which were dropped, and **offer to
  continue** on a specific range (e.g. "covered pp. 1–23 (skipped blank p. 4, p. 9); pp. 24–40 not
  shown — want those next?"). The agent stays in its conversational loop; the user stays informed and
  in control.

The selected page subset is shared across both Tier-2 deliveries: for the native block, build a
sub-PDF of the selected pages (`pypdfium2.PdfDocument.import_pages`) when the document exceeds the
provider limit — or, as a robust fallback if sub-PDF export is unavailable for a given input, deliver
those selected pages via the rasterize path instead; for the rasterize path, render exactly those
pages.

### 5. Guardrails (fail-closed)

Enforced server-side at resolution: page-count cap (§4); per-page rasterization DPI / dimension cap
with downscale (Pillow); total per-turn payload cap (respect the 32 MB / 100-page provider limits);
and a cap on extracted-text size (very large text layers are trimmed *with disclosure*, never sent
unbounded). Over-limit inputs are transformed below the cap or handled by the §4 selection — never sent
unbounded, never silently dropped.

### 6. Clean task description (inherits the ADR-0101 / FRE-661 invariant)

Extracted document text and document metadata travel in the structured attachment/resolution path —
**never** concatenated into `ctx.user_message`. Captain's Log `task_description` and entity extraction
read the user's original text byte-for-byte; the (potentially large) extracted PDF text does not
pollute self-improvement data or the knowledge graph.

### 7. User path control, cost tracking, and joinability

**7a. Per-attachment cloud/local override.** `AttachmentRef` carries an optional
`processing_target: Literal["cloud", "local"] | None`:

- `None` (default) → follow the conversation's bound ExecutionProfile (ADR-0044) — local profile
  rasterizes to the local Qwen; cloud profile uses the native PDF block.
- `"local"` → force local handling (text extraction, or rasterize → local Qwen vision). It **never
  escalates to cloud**, even on a profile that permits escalation — a document the user marked local
  never crosses the data-egress boundary; if the local model cannot read it, it fails closed with
  `AttachmentUnsupported` (§3).
- `"cloud"` → force the cloud native PDF block. This is the **only** way a local-profile conversation
  sends a document to cloud, and it is explicit and **still subject to the cost gate** (§7b).

The PWA exposes the override per attachment; the default (`None`) requires no user action.

**7b. Cloud pricing in the cost matrix + pre-flight estimate + threshold confirmation.** Cloud Claude
model definitions carry per-token pricing in the cost matrix (`config/models.cloud.yaml`). Before any
cloud document call, resolution computes a **pre-flight cost estimate** — `selected_pages ×
per-page-token estimate (text + image) × cloud price` — and **reserves it against the ADR-0065 cost
gate** before spending. Then:

- estimate ≤ configured threshold → proceed and meter.
- estimate > threshold → the agent **discloses the estimate and asks the user to proceed** (or keep it
  local/free), mirroring the §4 page-budget disclose-and-offer pattern. No spend until confirmed.

Actual cost is reconciled at commit via `litellm.completion_cost()`. Local turns are metered too (for
budget/observability consistency), though local compute carries no per-token charge.

**7c. Joinability (ADR-0074).** The document-resolution path threads `trace_id`, `session_id`, and
`task_id` onto the `store.get` byte fetch, the cost-gate reservation/commit, and every resolution /
routing / selection telemetry event. A document turn's cost row and resolution events **join back to
the turn** via `(trace_id, session_id, task_id)` — verified by the ADR-0074 joinability probe, no
orphan rows.

### Scope (v1)

`application/pdf`, **current-turn attachments only**. Tier 1 (text extraction) + Tier 2 (native PDF
block on cloud / rasterize on local), capability-routed and fail-closed; density+structure page
selection with disclose-and-offer-to-continue; per-attachment cloud/local override; pre-flight cost
estimate with threshold confirmation; trace/session/task joinability on cost and resolution events.

**Out of scope (deferred):** query-relevance page ranking (ColPali / DSE page-image embeddings) to pick
pages by question relevance — **flagged for future exploration (v2)**, owner-confirmed; multi-call
**chunking** of very large PDFs across model calls with synthesis; a **`force_strategy: text|vision`**
override to force vision on a native-text PDF; **hybrid both** (text + page-images in one turn) for
figure-heavy native-text PDFs; OCR of a native-text PDF's *figures* beyond what Tier 1
text or Tier 2 vision already covers; non-PDF document types (docx, pptx, xlsx); examining an
*arbitrary previously-stored* document mid-conversation (the tool-result path noted in ADR-0101's
Alternatives).

---

## Alternatives Considered

### Option 1: Always send the native provider PDF block (no text-first tier)

**Description:** For every PDF, hand the model the provider-side native PDF document block; never do
server-side text extraction.

**Pros:**
- Highest out-of-the-box fidelity (text + image per page); least code; one delivery path on cloud.

**Cons:**
- ~7× the token cost of text extraction on the common native-text case (per Anthropic's documented
  per-page text+image charging) — pays for vision on documents where extraction is *equally accurate*.
- Cloud-only — the local SLM has no native-PDF-block equivalent, so this provides **no local story**
  and silently abandons the local-first posture.
- Bound by the provider's 100-page / 32 MB ceiling with no room for our own cheaper handling.

**Why Rejected:** Strictly more expensive on the dominant case for no accuracy gain, and it has no
answer for the local profile. Text-first reserves the costly path for when vision is actually needed.

### Option 2: Rasterize-to-images everywhere (uniform path, reuse ADR-0101 exactly)

**Description:** Render every PDF's pages to images on both profiles and send image blocks — one code
path, identical to the ADR-0101 image flow.

**Pros:**
- Single uniform path; no native-PDF-block flag; reuses ADR-0101 verbatim; portable to any vision model.

**Cons:**
- Pays per-page vision cost even for native-text PDFs where extraction is equal-accuracy and far
  cheaper, and discards the text layer's losslessness/searchability.
- Per the token-efficiency research, rendering dense text as images degrades sharply past a
  "text-token tolerance" (worse on small fonts / dense pages / smaller decoders) — the local 35B
  primary is exactly where that bites.
- We own OCR-via-vision quality instead of leaning on the provider's tuned native pipeline on cloud.

**Why Rejected:** Uniformity at the cost of money and fidelity on the common case. Tier 1 + the native
block (cloud) capture both ends better.

### Option 3: Text-extraction only, never vision

**Description:** Always extract the text layer; never rasterize or use the native block.

**Pros:**
- Cheapest; one dependency; no vision capability required anywhere.

**Cons:**
- Blind to scanned/image-only PDFs (empty text layer → nothing to read) and to figures, tables, charts,
  and layout — the exact content vision exists to recover. Reproduces a silent-failure mode: an empty
  extraction sent as text yields a confidently wrong answer.

**Why Rejected:** Fails the scanned case entirely and silently. Tier 2 exists precisely for it.

### Option 4: PyMuPDF (`fitz`) for extraction + rasterization

**Description:** Use the mature, fast PyMuPDF for both text and rendering — one well-known library.

**Pros:**
- Single dependency, excellent text + render quality and performance.

**Cons:**
- **AGPL-3.0** (strong copyleft) — disqualified for this project absent a paid Artifex commercial
  license; would impose copyleft obligations on the codebase.

**Why Rejected:** Licensing. `pypdfium2` (Apache-2.0/BSD-3) provides the same two capabilities — text
extraction *and* rasterization — from a permissive, self-contained wheel.

### Option 5: pdf2image + Poppler for rasterization

**Description:** Rasterize with `pdf2image`, which shells out to Poppler's `pdftoppm`/`pdftocairo`.

**Pros:**
- MIT Python wrapper; mature, high-quality rendering.

**Cons:**
- Poppler is **GPL**; usable via `subprocess` as mere aggregation, but redistributing the Poppler
  binary in our Docker image carries a GPL compliance burden (source-availability obligations), and it
  adds a system-binary dependency.

**Why Rejected:** Unnecessary GPL burden once `pypdfium2` covers rasterization with no system binary
and a permissive license.

### Option 6: Query-relevance page retrieval (ColPali / DSE) in v1

**Description:** Embed the user's question and each page (text or page-image embeddings) and send the
top-K most relevant pages within budget.

**Pros:**
- Research-backed (ColPali, DSE) and more accurate at picking pages than density heuristics.

**Cons:**
- Adds an embedding-model dependency + serving infra; the retrieval framing fits the *bulk RAG index*,
  not the single in-context upload, where density+structure selection already captures most of the
  value at zero new infra.

**Why Rejected (for v1):** Disproportionate for a first document path; **deferred to v2 as
owner-confirmed future exploration**. Density + outline/TOC selection ships the clever-budget value now.

---

## Consequences

### Positive Consequences

- The agent reads uploaded PDFs — closing the document half of attachment ingestion on top of the
  ADR-0101 foundation, with no foundation rework.
- Cost-optimal by construction: the common native-text PDF takes the cheap text path; the ~7× vision
  path fires only for scanned/low-text PDFs.
- License-clean: `pypdfium2` + `Pillow` add PDF text + rasterization with **no copyleft and no GPL
  system binary** — a deliberate, verified licensing outcome.
- Capability-routed and fail-closed, consistent with ADR-0099's config-single-source posture and
  ADR-0101's routing: the native-PDF-block flag records deployed reality; no hardcoded model names.
- No new data-egress boundary: the document travels with the profile the user already chose; local
  PDFs are read on the local SLM via rasterization, never silently sent to cloud. A `"local"` override
  hardens this; a `"cloud"` override is the only crossing and is explicit + cost-gated.
- The page-budget path informs rather than silently truncates — the user always knows what was read and
  can ask for more.
- Cost is controlled *before* the spend, not just after: cloud documents are estimated, reserved
  against the cap, and confirmed past a threshold — the user is never surprised by an expensive PDF.
- Cost and resolution events are joinable (trace/session/task), so a document turn's spend and routing
  are attributable end-to-end via the existing ADR-0074 probe.

### Negative Consequences

- **First PDF/imaging dependencies** (`pypdfium2`, `Pillow`) enter the build, with a native PDFium
  wheel per platform. Mitigated by their self-contained, permissive packaging (no system binary).
- **Figure-heavy native-text PDFs get text only in v1.** A born-digital PDF with a usable text layer
  routes to Tier 1, so important *figures/diagrams* are not visually read. **v1 has no force-vision
  control** for a native-text PDF (`processing_target` governs cloud/local placement, not Tier-1 vs
  Tier-2 strategy); a `force_strategy: text|vision` override and the "hybrid both" path are deferred to
  v2. Accepted v1 limitation; surfaced as a risk below.
- **Cost — vision-tier PDFs are expensive.** Native block ≈ 7× text; rasterized pages scale per page.
  The cost gate (ADR-0065) must meter document image/page tokens, not just text. The page budget bounds
  it.
- **Routing now branches on document capability too.** Tier-2 resolution reads `supports_pdf_document`
  in addition to `supports_vision`; the selector adds page-inspection work at turn assembly.
- **Text-density floor is a heuristic.** A PDF with a thin/garbage text layer (bad OCR baked in) may be
  mis-classified as native-text; see Risks.
- **More moving control surface.** A new `processing_target` field on the carrier (+ a PWA affordance),
  a pre-flight cost estimator, a configurable cost threshold, and trace/session/task threading on the
  document path — each is small, but together they widen the change beyond the bare resolution logic.
- **Pre-flight estimate is approximate.** The reservation uses a per-page token estimate; actual cost
  is reconciled at commit, so the reservation may over- or under-shoot. Mitigated by reconciliation and
  by the hard `cap_usd` ceiling.

### Risks and Mitigations

| Risk | Severity | Mitigation |
|------|----------|------------|
| A scanned PDF yields an empty/near-empty text layer that is sent as text → confident wrong answer | High | The text-density floor routes low-text PDFs to Tier-2 vision; AC-2 feeds a scanned PDF and asserts the vision path is taken, not an empty text block |
| A copyleft (AGPL/GPL) PDF dependency or system binary slips in | High | Standardize on `pypdfium2` + `Pillow`; AC-3 asserts no `pymupdf`/`fitz`/`pdf2image`/poppler in the Python deps **and** no poppler/GPL renderer binary in the Docker image, and no AGPL/GPL in the PDF stack |
| A non-capable model silently receives a document and hallucinates | High | Tier-2 routing asserts `supports_pdf_document` or `supports_vision` (and validates the escalation target's capability); else raise `AttachmentUnsupported` (AC-6) — fail-closed, never silent |
| Large PDF blows context / cost or is silently truncated | Medium-High | Page budget = min(cap, provider limit); density+structure selection within budget; disclose included/dropped pages + working continuation (AC-4); per-page raster + total payload + extracted-text caps fail-closed (AC-7) |
| Figure-heavy native-text PDF: figures not visually read in v1 | Medium | Documented v1 limitation; no force-vision control in v1 (`force_strategy` override deferred to v2 alongside "hybrid both") |
| Mis-classified text layer (thin/garbage text → treated as native) | Low-Medium | Density floor tuned conservatively; the floor and caps are config (ADR-0099), adjustable without code change |
| Selected-page sub-PDF for the native block loses cross-page context | Low | Selection prefers contiguous substantive ranges and discloses the dropped ranges so the user can request more |
| A `"cloud"` override (or cloud profile) silently runs up cost on a big PDF | Medium | Pre-flight estimate + reservation against `cap_usd`; threshold confirmation before spend, proceed only after confirm (AC-9); actual reconciled at commit (AC-10) |
| A `"local"` override is silently escalated to cloud (boundary breach) | High | Override is honored strictly: `"local"` never escalates — fail-closed to `AttachmentUnsupported` instead (AC-8) |
| Cloud Claude document pricing missing → spend metered as $0 (silent under-billing) | Medium | Cloud pricing asserted present in the cost matrix; non-zero committed cost asserted (AC-10) |
| Document cost/resolution events orphaned (not joinable to the turn) | Medium | Thread `trace_id`/`session_id`/`task_id` through resolution + cost; ADR-0074 probe asserts zero orphans (AC-11) |

---

## Implementation Notes

**Files affected:**

- New document-resolution module (sits alongside / within the ADR-0101 attachment-resolution module) —
  text extraction (`pypdfium2`), text-density classification, page selection (density + outline), native
  PDF document-block construction, rasterize-to-image-blocks (`pypdfium2` + `Pillow`), guardrails.
  Designed as the document branch the ADR-0101 resolver was shaped to accept.
- `llm_client/models.py` — add `ModelDefinition.supports_pdf_document: bool`.
- `config/models.yaml` — set `supports_pdf_document` (true for `claude_sonnet`, `claude_haiku`; false
  for `primary`, `sub_agent`).
- `orchestrator/executor.py` — Tier-2 routing reads `supports_pdf_document` alongside `supports_vision`
  at the ADR-0101 routing seam; turn-assembly injection of the document content block(s).
- `tools/artifact_tools.py` — binary-path honesty already established by ADR-0101 applies to PDFs.
- `pyproject.toml` / `uv.lock` — add `pypdfium2`, `Pillow`.
- Cost gate (`cost_gate/`) — pre-flight document cost estimator (pages × per-page tokens × price) →
  reservation; meter native-PDF / rasterized image+page tokens at commit (extends the ADR-0101 image
  metering).
- `AttachmentRef` carrier (the ADR-0101 carrier) — add `processing_target: Literal["cloud","local"] |
  None`; orchestrator routing reads it ahead of the profile default and enforces the `"local"`
  no-escalation rule.
- `config/models.cloud.yaml` — ensure cloud Claude per-token pricing is present for the document path;
  `config/` — the cost-confirmation threshold (ADR-0099 config-single-source).
- Document path telemetry — thread `trace_id`/`session_id`/`task_id` onto resolution, selection, cost,
  and routing events (ADR-0074 joinability).
- PWA (`seshat-pwa/`) — per-attachment cloud/local override affordance (defaults to none).

**Dependencies (prerequisites, not yet implemented):** the **full ADR-0101 chain must land first**
(carrier, credentialed fetch, content widening, turn-assembly seam, `supports_vision`) — ADR-0101 is
Proposed and its tickets are unapproved as of this writing; R2 store (ADR-0069); cost gate (ADR-0065).

**Testing strategy:** unit tests over the document-resolution module (text-density classification;
page selection skipping a synthetic blank page; native-block vs rasterize delivery by capability;
guardrail caps) with a mocked `store` and synthetic PDFs; a routing test for the `supports_pdf_document`
/ `supports_vision` precedence and fail-closed branch; an assertion over assembled `request_messages`
for the right block type per tier; a license-cleanliness check over the dependency set; and a master
live smoke for native-text → text and scanned → vision end-to-end.

---

## Verification / Acceptance Criteria

**How will we know this decision actually delivered — not just merged?**

- **AC-1 (native-text PDF read via cheap text path, not images)** — A `/chat` turn with a native-text
  PDF produces a response conditioned on the document's text *and* is delivered as extracted text, not
  rasterized images. **Check:** a live turn with a test PDF whose text layer contains a unique sentinel
  string; assert the response references the sentinel **and** assert the assembled `request_messages`
  for that turn contain a **text** block from extraction and **no** image/document block. *Fails if* a
  native-text PDF is rasterized/sent as a vision block (cost regression) or the sentinel is absent.
- **AC-2 (scanned PDF routes to vision, not an empty text block)** — A scanned/image-only PDF (no usable
  text layer) is delivered via the vision tier and the response is conditioned on visual content.
  **Check:** feed a scanned test PDF (empty text layer) with a sentinel visible only in the page image;
  assert the response references the sentinel **and** assert the assembled request used a native PDF
  document block (cloud) or image blocks (local) — **not** an empty/near-empty text block. *Fails if*
  the empty extraction is sent as text and the model is left to hallucinate.
- **AC-3 (license-clean: Python deps AND system binaries)** — No copyleft PDF dependency *or* GPL
  renderer binary ships. **Check:** assert `pyproject.toml` + `uv.lock` contain `pypdfium2` and
  `pillow` and contain **no** `pymupdf` / `fitz` / `pdf2image` / poppler; run a license check over the
  resolved environment and assert the PDF stack carries no AGPL/GPL license; **and** scan the built
  Docker image (`docker/`, installed system packages) and assert **no** `poppler`/`poppler-utils` or
  other GPL PDF-renderer binary is installed. *Fails if* PyMuPDF or a GPL renderer is present in the
  Python deps **or** a GPL renderer binary is baked into the image (the §2 "no GPL system binary"
  promise).
- **AC-4 (over-budget: bounded, disclosed, and continuation actually works)** — For a PDF whose pages
  exceed the budget, the turn sends only pages within budget, the response names included vs dropped
  page ranges, **and a follow-up request for a dropped range actually delivers those pages**. **Check:**
  feed a PDF with N > budget pages (including a known blank page); assert the assembled request's
  page/image count ≤ budget and the response enumerates included **and** dropped page ranges; then issue
  a follow-up turn requesting a dropped range for the same artifact and assert that turn's assembled
  request contains exactly those previously-omitted pages. *Fails if* pages beyond budget pass through,
  the included/dropped disclosure is omitted (silent truncation), **or** the offered continuation is a
  dead end (the follow-up does not surface the omitted pages).
- **AC-5 (selection skips low-information pages)** — Given a budget too small to hold every page, the
  selector keeps an informative page over a near-blank one, scored by concrete proxies (extracted text
  character count per page and/or non-white pixel-coverage ratio). **Check:** a unit test over the
  selector with a synthetic page set — one near-blank page (text chars below the configured floor, e.g.
  < 20, and pixel coverage near zero) and one dense page (chars well above the floor) — and budget = 1
  asserts the dense page is selected and the blank page is dropped. *Fails if* selection is naive
  first-N and keeps the below-floor page over the above-floor one.
- **AC-6 (vision tier is fail-closed by capability, escalation target included)** — A scanned PDF on a
  profile whose available models (including any configured escalation target) are neither vision- nor
  native-PDF-capable produces a clear `AttachmentUnsupported` error — never a silent text-only or
  hallucinated answer, and no document/image block ever reaches a non-capable model. **Check:** test (a)
  a local profile with `supports_vision=false` / `supports_pdf_document=false`, no escalation → feed a
  scanned PDF → assert `AttachmentUnsupported` raised and no document/image block handed to any model;
  test (b) a profile that *permits* escalation but whose escalation model is itself non-capable → assert
  it fails closed rather than escalating to the incapable model. *Fails if* a non-capable model (primary
  or escalation target) receives the document or a degraded answer is returned.
- **AC-7 (every guardrail dimension fails closed)** — For **each** configured Tier-2 cap — per-page
  rasterization pixel dimension, per-page image byte size, total per-turn payload, and extracted-text
  size — an over-limit input is either transformed below the limit (downscaled / trimmed *with
  disclosure*) or rejected; the over-limit bytes never reach the model. **Check:** one parametrized test
  per cap dimension feeds an input exceeding that specific cap and asserts (a) the resolved block is
  below the cap or the turn is rejected, and (b) the over-limit content is absent from the assembled
  `request_messages` (and, for trimmed text, the response discloses the trim). *Fails if* any single
  dimension (e.g. the total payload, or extracted-text size) passes through unbounded while another is
  enforced.
- **AC-8 (per-attachment override honored, fail-closed)** — A `"local"` `processing_target` never
  reaches cloud even on an escalation-permitted profile; a `"cloud"` override routes to the native PDF
  block and is cost-gated. **Check:** test (a) `processing_target="local"` on a cloud-escalation-enabled
  profile with a scanned PDF and a non-vision local model → assert **no** cloud call is made and
  `AttachmentUnsupported` is raised (not a silent escalation); test (b) `processing_target="cloud"` from
  a local-profile conversation → assert the native PDF block path is taken **and** a cost-gate
  reservation was made. *Fails if* a `"local"`-marked document reaches cloud, or a `"cloud"`-marked
  document bypasses the cost gate.
- **AC-9 (pre-flight estimate gates spend, and confirm actually proceeds)** — A cloud document whose
  estimated cost exceeds the threshold does **not** call the model until the user confirms; on
  confirmation it **does** proceed; an under-threshold document proceeds directly. **Check:** (a) feed a
  cloud PDF whose estimate exceeds the threshold → assert **no** model call is issued and the response
  carries the dollar estimate + a proceed/keep-local prompt; (b) supply the confirmation → assert the
  model **is** then called and the spend is committed; (c) feed an under-threshold document → assert it
  proceeds and a cost-gate reservation ≈ the estimate is recorded *before* the call. *Fails if* an
  over-threshold cloud document calls the model without disclosure, no reservation precedes the spend,
  **or** a confirmed document never proceeds (a dead-end prompt).
- **AC-10 (cloud documents are priced and metered, not free)** — Cloud Claude document pricing is present
  in the cost matrix and the committed cost is non-zero, reconciled from actual usage, and includes the
  vision (image/page) token component for a scanned PDF. **Check:** assert the cloud model definition in
  `config/models.cloud.yaml` carries per-token pricing; for a cloud scanned-PDF turn assert the
  cost-gate `commit` records a non-zero `actual_cost` from `litellm.completion_cost()` whose token
  basis includes image/page tokens (not text-only). *Fails if* cloud document pricing is missing
  (metered as $0), the spend is never committed, or a vision-tier PDF is metered as text-only.
- **AC-11 (joinability — no orphan cost/resolution rows)** — A document turn's cost row and resolution
  events carry `trace_id` + `session_id` + `task_id` and join back to the turn. **Check:** after a
  document turn, run the ADR-0074 joinability probe (`observability/` joinability probe) and assert the
  cost-gate row and the resolution/selection telemetry join to the turn's
  `(trace_id, session_id, task_id)` with **zero** orphans. *Fails if* any document cost or resolution
  event lacks a join key or is orphaned per the probe.
- **AC-12 (clean task description — inherits the FRE-661 invariant for documents)** — For a PDF
  attachment turn, the captured task text equals the user's original submitted message **byte-for-byte**
  and no extracted document text or metadata leaks into it. **Check:** assert
  `TaskCapture.user_message` (`captains_log/capture.py`) and any persisted task description are
  byte-for-byte equal to the original submitted text; assert no extracted PDF text, artifact ID, or
  filename appears there, and that attachment metadata is present only in the structured carrier.
  *Fails if* the captured text differs from the original by any byte — in particular if extracted
  document text is concatenated into it.

**Seam owner (decomposed ADR) — the single distinguished seam criterion is AC-SEAM, below.** The
per-ticket ACs above each fall to one child ticket; **AC-SEAM can only pass once every child has
landed** and is owned by the **final document live-smoke ticket**, run by master at the integration
gate:

- **AC-SEAM (end-to-end, the whole pipeline in one live run)** — In a single live session: (1) a
  native-text PDF returns an answer conditioned on its text delivered as a **text** block (not images);
  (2) a scanned PDF returns an answer conditioned on **visual** content via the profile's vision path;
  (3) the same scanned PDF under a `"local"` override on an escalation-permitted profile with no capable
  local model **fails closed** with `AttachmentUnsupported` (no cloud crossing); (4) a scanned PDF under
  a `"cloud"` override **whose pre-flight estimate exceeds the threshold** is held until confirmation,
  then on confirmation proceeds via the native PDF block and **commits a non-zero cost whose token
  basis includes image/page tokens** (exercising AC-8 override + AC-9 confirmation + AC-10 cloud
  metering together); **and** (5) the ADR-0074 joinability probe reports zero orphans for all four
  turns. **Check:** master runs all five legs against the live stack. *Fails if* any leg regresses —
  which it will unless the carrier (ADR-0101 chain), library swap, capability flags, selector, override,
  **cloud cost gate (estimate + confirm + meter)**, and joinability threading have **all** landed. The
  ADR does **not** close because the last child ticket merged in isolation — only AC-SEAM closes it.

---

## References

- ADR-0101 — Agent Vision Ingestion of Uploaded Images (the foundation: carrier, credentialed fetch,
  content widening, turn-assembly seam, capability routing, `supports_vision`)
- ADR-0069 — R2 artifact substrate (the `store.get` credentialed byte path)
- ADR-0070 — output channels (human-facing `public_url` display)
- ADR-0099 — configuration management & validation (config-single-source for capability flags + cost threshold)
- ADR-0065 — cost gate (pre-flight reservation + commit; must meter document image/page tokens)
- ADR-0044 — execution profiles (cloud/local binding; the per-attachment override defaults to it)
- ADR-0074 — joinability (trace/session/task threading; the probe that asserts no orphans)
- ADR-0033 — model role taxonomy (routing seam)
- FRE-369 — upload UX, live (PDFs already accepted by the allowlist)
- FRE-662 — image-ingestion tracking issue (ADR-0101)
- FRE-667 — this ADR's authoring issue (carved out of ADR-0101)
- Anthropic PDF support — https://platform.claude.com/docs/en/build-with-claude/pdf-support (limits,
  per-page text+image token model, transport, caching)
- pypdfium2 (Apache-2.0/BSD-3; text + render) — https://github.com/pypdfium2-team/pypdfium2
- ColPali — https://arxiv.org/abs/2407.01449 ; DSE — https://arxiv.org/abs/2406.11251 (deferred v2
  query-relevance page retrieval)
- "Text or Pixels? It Takes Half" — https://arxiv.org/html/2510.18279v1 (visual-text token efficiency
  and degradation thresholds)
- Code anchors — `service/uploads_router.py:53,68`, `llm_client/models.py`, `config/models.yaml`,
  `orchestrator/executor.py` (ADR-0101 routing seam), `tools/artifact_tools.py`, `cost_gate/`

---

## Status Updates

### 2026-06-29 - Proposed
**Changed By:** lextra (adr session, Opus)
**Reason:** Design pass for FRE-667, carved out of ADR-0101. Tiered text-first / vision-on-demand PDF
ingestion on the ADR-0101 foundation; license-clean `pypdfium2` + `Pillow` stack (PyMuPDF AGPL and
pdf2image/Poppler GPL rejected); capability-routed Tier-2 delivery (native PDF block on cloud /
rasterize on local) with a new `supports_pdf_document` flag; density+structure page selection with
disclose-and-offer-to-continue. Research-grounded (frontier-vendor hybrid behavior; ColPali/DSE scoped
to retrieval; Anthropic per-page cost). Adds owner-requested controls: per-attachment cloud/local
override (fail-closed on `"local"`), pre-flight cost estimate + threshold confirmation reserved against
the ADR-0065 cap, cloud pricing in the cost matrix, and ADR-0074 trace/session/task joinability on
cost + resolution events. (The same control set should be folded into ADR-0101's still-unapproved image
tickets — owner-flagged.) Query-relevance ranking, chunking, and hybrid-both deferred to v2.

---

**Template Version:** 1.1
**Based On:** [Michael Nygard's ADR pattern](https://cognitect.com/blog/2011/11/15/documenting-architecture-decisions)
