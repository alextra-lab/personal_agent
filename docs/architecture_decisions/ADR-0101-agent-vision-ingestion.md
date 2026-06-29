# ADR-0101: Agent Vision Ingestion of Uploaded Images

**Status:** Proposed
**Date:** 2026-06-28
**Deciders:** lextra (owner), Seshat architecture
**Tags:** uploads, vision, multimodal, model-routing, orchestrator, r2

---

## Context

**What is the issue we're addressing?**

FRE-369 shipped end-to-end image upload UX (PWA presigns a PUT to R2, the gateway persists an
`artifacts` row). The live smoke test exposed the next gap: **the upload succeeds, but the agent
cannot see the image.** The agent reported the artifact URL returned a Cloudflare-Access login page
instead of the JPEG.

The failure is a chain of three layers, each verified in code:

1. **Upload stores bytes in R2.** `uploads_router.py` persists the `artifacts` row with the
   browser-declared `content_type` (the upload allowlist at `uploads_router.py:46-59` includes
   `image/png|jpeg|gif|webp`, `image/svg+xml`, `application/pdf`, and text types — this ADR's v1 scope
   is the raster image subset; `application/pdf` is owned by ADR-0102); the bytes land in R2 via the
   presigned PUT. The metadata canon is Postgres (`docker/postgres/init.sql:349-365`).
2. **Attachment is flattened to a text pointer.** `service/app.py:187-211`
   (`_augment_message_with_attachments`) *prepends a plain-text block* to the user message —
   `"[Attachments — call artifact_read(artifact_id) to read content:]"` — and passes the augmented
   string as `user_message`. The model receives a sentence, not an image. (This augmentation also
   pollutes Captain's Log `task_description` and entity extraction — the problem FRE-661 was filed to
   fix.)
3. **`artifact_read` cannot return image bytes, and its URL is unreachable.** `artifact_read`
   (`tools/artifact_tools.py:672-690`) fetches bytes inline only for *textual* types ≤256 KB. For
   binary/image types it returns metadata + `public_url`, `content: None`, **no bytes**. The
   `public_url` (`artifact_tools.py:267-271`) is the `artifacts.frenchforet.com/{id}` route, which is
   **guarded by Cloudflare Access** — so when the agent fetches it, CF Access returns a login page.
   The agent never receives the image content.

Two distinct missing pieces, and a deeper truth behind both:

- **Fetch bytes server-side.** The credentialed path already exists: `store.get(r2_key)`
  (`storage/artifact_store.py:222-253`) is the direct R2 S3 API — credentialed, **bypasses Cloudflare
  Access**, returns raw bytes. It is wired for textual artifacts only, never for binary.
- **Deliver as a vision content block to a vision-capable model.** Returning raw or base64 bytes in a
  tool result to a model does **not** let it see the image — a model "sees" an image only when the
  bytes arrive as a typed image content block in the message. Plain or base64 text is just text.

**What needs to be decided:**

- **Where do image bytes enter the model call** — resolved into a content block at *turn assembly*
  (before the first model call), or via a tool the model invokes mid-turn?
- **Which model handles the turn, and how does routing guarantee a vision-capable model** when an
  attachment is present?
- **How this composes with FRE-661** (thread structured attachments through `handle_user_request`).
- **Fix the broken `public_url`-for-binaries path** for agent consumption.

**Grounding facts (verified against config + owner):**

- The wire message format already supports content blocks: `content` is `list[dict[str, Any]]`-shaped
  and the Anthropic prompt-cache plumbing already sends `content` as a *list of blocks*
  (`litellm_client.py:57-61`). An `image_url` block flows end-to-end on the cloud path without a type
  fight. The first schema constraint to widen is the persisted `Message` model
  (`service/models.py:130`, `content: str`); a broader str-assuming surface is enumerated under
  Negative Consequences.
- `handle_user_request` (`orchestrator/orchestrator.py:38`) carries **no** `attachments` parameter;
  `ExecutionContext` carries none; the new user turn is appended as a bare string
  (`executor.py:1741`). The structured carrier does not exist yet — this is exactly FRE-661's scope.
- `_determine_initial_model_role` (`executor.py:1309`) always returns `PRIMARY`; the model is
  profile-bound. There is **no `supports_vision` flag** in `ModelDefinition`
  (`llm_client/models.py`) or `config/models.yaml`.
- **Vision capability is a deployment property, not yet modeled in config.** Per the owner, the
  Qwen3.6-35B-A3B models (`primary`, `sub_agent`) are served by the SLM Server **remotely via the
  tunnel** (not on the VPS — the VPS hardware envelope is irrelevant to vision) and are vision-capable
  on the deployed build; the cloud Claude models (`claude_sonnet`, `claude_haiku`) are vision-capable.
  This is **not verifiable from the repository** — there is no capability metadata on
  `ModelDefinition` today, which is precisely the gap this ADR closes by adding a `supports_vision`
  flag that *records* the deployed reality. Routing keys off that declared flag, never off a hardcoded
  model name or an unverifiable assumption. The data-egress property holds independently of the flag:
  **an attached image follows the same trust boundary the turn's text already crosses** (it goes to
  whatever model the turn's profile already routes to — own SLM server on local, Anthropic on cloud),
  so vision introduces **no new data-egress boundary**.

---

## Decision

Deliver attachments to the model as **typed content blocks resolved at turn assembly**, routed to a
**model whose vision capability is asserted from config**. Concretely:

### 1. Turn-assembly resolution (not a tool call)

Current-turn attachments are resolved into content blocks **before the first model call** and injected
into the initial user message. This matches the dominant intent ("here is an image — look at
it"): the model sees the attachment on its very first call, with no extra round-trip. A post-hoc
`artifact_read` tool call is *not* the primary mechanism (see Alternatives).

### 2. Structured attachment carrier — folds FRE-661

Thread a structured `attachments: Sequence[AttachmentRef] | None` parameter through
`handle_user_request` → `ExecutionContext`, **separate from `ctx.user_message`**. `ctx.user_message`
stays the user's clean original text (Captain's Log + entity extraction read it); the attachment
metadata travels alongside. `AttachmentRef` is a frozen dataclass carrying at least
`{artifact_id, content_type, title, r2_key}`. `service/app.py` passes the validated structured list
instead of the augmented string; `_augment_message_with_attachments` (the text-pointer prefix) is
removed from the orchestrator path. **This subsumes FRE-661** — the carrier is the first ticket of
this ADR's chain.

### 3. Server-side credentialed byte fetch

At resolution, bytes are fetched via `store.get(r2_key)` (direct R2 S3, bypasses Cloudflare Access).
The agent is **never** handed the CF-Access `public_url` to fetch as a content source.

### 4. Content-block construction

A **raster image** (`image/png|jpeg|gif|webp`) → a single image block. Canonical input is the
OpenAI-style `image_url` block with a `data:` URI (base64); LiteLLM transforms it to the Anthropic
image-source block for the cloud path, and the local SLM path passes it through unaltered. An image is
one bounded visual payload — no pagination, no chunking; the only safeguard is a size/dimension cap
(§6).

**Documents (PDF) are out of scope for this ADR.** They are a categorically different problem —
variable page count, scanned-vs-native-text-layer (OCR vs vision), chunking, per-page cost, and a
text-extraction-vs-vision strategy decision — and are owned by **ADR-0102 (Document Ingestion,
forthcoming)**. ADR-0102 reuses this ADR's structured-attachment carrier (§2), credentialed fetch
(§3), turn-assembly injection, and capability routing (§5); it owns only the document-strategy layer.

### 5. Capability-driven routing

Add `supports_vision: bool` (raster images) to `ModelDefinition`, set on the model definitions in
`config/models.yaml` (true for `primary`, `sub_agent`, `claude_sonnet`, `claude_haiku`). (ADR-0102
will add any document-specific capability flag it needs, e.g. native-PDF support.) When a turn carries
an image attachment, the routing/turn-assembly layer **asserts the selected model supports vision**:

- If the selected model is capable → proceed (the expected common case once the flags are set, since
  the deployed primaries are vision-capable).
- If not capable and the active profile permits cloud escalation (`cloud.yaml`:
  `allow_cloud_escalation: true`, `escalation_model: claude_sonnet`) → escalate to the capable
  escalation model.
- If not capable and the active profile **forbids** escalation (`local.yaml`:
  `allow_cloud_escalation: false`, no escalation model) → there is **no silent fallback and no
  cross-boundary escalation**: the turn **fails fast with a clear, user-visible `AttachmentUnsupported`
  error** naming the unsupported modality. The local-first boundary is never crossed implicitly to
  service an attachment.

`ExecutionContext` gains the attachment metadata so the routing seam (`_determine_initial_model_role`
/ its successor) can read it. The text-pointer augmentation is **not** retained as a degraded fallback
(that would silently reproduce the FRE-369 failure); an unsupported/mismatched attachment is rejected
explicitly, not quietly downgraded.

### 6. Guardrails (fail-closed)

Server-side caps enforced at resolution: per-image max dimension and byte size (downscale or reject
oversized), max images per turn, and a total per-turn attachment payload cap. Oversized inputs are
downscaled below the cap or rejected with a clear error — never sent unbounded. (Document-specific
guardrails — page caps, chunking — belong to ADR-0102.)

### 7. Fix the broken `public_url` path

`artifact_read`'s binary path stops presenting `public_url` as a fetchable content source for the
agent (the agent cannot pass CF Access). For image artifacts it states the bytes are delivered via the
turn's content block (current-turn attachments) and are not URL-fetchable by the agent. `public_url`
remains for **human / output-channel display** (PWA, ADR-0070 rich output) only.

### Scope (v1)

Raster `image/png|jpeg|gif|webp`, **current-turn attachments only**. Out of scope: **documents (PDF) —
ADR-0102 (forthcoming)**; SVG (XML, not a raster — Anthropic does not accept it as an image);
examining an *arbitrary previously-stored* image mid-conversation (the tool-result-image-block path —
see Alternatives); audio/video.

---

## Alternatives Considered

### Option 1: Tool-result image blocks (model calls `artifact_read` → image block in the tool result)

**Description:** Keep delivery model-driven. The model calls `artifact_read(artifact_id)` on an image;
the tool result carries a typed image block (Anthropic supports image blocks inside `tool_result`
content), which a vision-capable model sees on its *next* call.

**Pros:**
- Works for *any* stored image — past turns, not just the current upload.
- Reuses the existing tool; no turn-assembly plumbing or `ExecutionContext` change.
- Routing can be decided at the moment the model chooses to look.

**Cons:**
- Extra round-trip: the model must first *decide* to call the tool, then see the image only on the
  following call — poor fit for the dominant "here's an image, what is it?" intent.
- The model deciding to call `artifact_read` might be a non-vision model; switching to a vision model
  mid-turn for the tool result is awkward and under-specified.
- Local OpenAI-compatible servers may not accept image blocks inside `tool_result`.

**Why Rejected:** Wrong default for *uploaded* images, which the user attaches precisely so the agent
will look now. **Documented as the future path** for "examine an arbitrary stored image" (a deferred
v2 capability) — the two mechanisms are complementary, not competing.

### Option 2: Return base64 bytes in the tool result to the current (possibly text) model

**Description:** Wire `artifact_read` to return the raw/base64 image bytes as text in the tool result.

**Pros:**
- Trivial to wire (`store.get` already returns bytes).

**Cons:**
- A model cannot see an image from base64 *text* — vision requires a typed image block, not a text
  field full of base64. This does not actually deliver vision.

**Why Rejected:** It does not solve the problem; it only makes the failure quieter (the model
hallucinates over gibberish instead of reporting it cannot see the image).

### Option 3: Force all image turns to cloud Anthropic

**Description:** When an image attachment is present, always route to `claude_sonnet`.

**Pros:**
- Simplest routing rule; Anthropic has strong vision.

**Cons:**
- Unnecessary now that the SLM-server Qwen models are vision-capable.
- Crosses a data-egress boundary the local-profile user did not choose — the image leaves the owner's
  own infrastructure to a third party purely as a routing artifact.
- Abandons the local-first posture for a capability the local profile already has.

**Why Rejected:** Capability-driven routing keeps the attachment on whatever profile the user already
selected; the image follows the same trust boundary as the turn's text. Forcing cloud is a strictly
worse privacy/locality outcome with no compensating benefit.

### Option 4: One ADR covering both images and documents (PDF)

**Description:** Keep the original single-ADR scope — images *and* PDFs handled in one decision and one
implementation chain.

**Pros:**
- One carrier, one resolution module, one ADR to track; images and documents share most plumbing.

**Cons:**
- Documents carry a genuinely different decision shape — variable page count, scanned-vs-native text
  layer (OCR vs vision), chunking, per-page cost scaling, and a *text-extraction-vs-vision* strategy
  choice that may mean **not using vision at all** for text-layer PDFs. Folding that in buries the
  decisions and couples the simple, high-value image fix to the harder document design.
- The live bug (FRE-369 smoke test) was an **image** — bundling delays its fix behind the document
  work.

**Why Rejected:** Documents merit their own ADR (**ADR-0102**). The split keeps a clean
foundation-vs-strategy boundary: this ADR delivers the shared foundation (carrier, credentialed fetch,
content widening, capability routing) plus the simple image path; ADR-0102 reuses that foundation and
owns the document-strategy layer. The image fix ships without waiting on OCR/chunking design.

### Option 5: OCR / caption the image server-side, feed text to a text model

**Description:** Run OCR or a captioning model server-side and inject the extracted text.

**Pros:**
- No vision model needed in the main loop.

**Cons:**
- Lossy and not "seeing" — fails for anything OCR/caption misses (diagrams, layout, non-text
  content); adds an OCR/caption dependency and a second model hop.

**Why Rejected:** Defeats the purpose when vision-capable models are already available on both
profiles.

---

## Consequences

### Positive Consequences

- The agent actually sees uploaded images — closing the FRE-369 image gap without waiting on the
  harder document design.
- Bytes are fetched over the credentialed R2 S3 path; the Cloudflare-Access failure mode is
  eliminated for agent consumption.
- Captain's Log `task_description` and entity extraction stay clean (FRE-661 folded in) — no synthetic
  attachment preamble pollutes self-improvement data or the knowledge graph.
- Vision capability is modeled in config (`supports_vision`), consistent with ADR-0099's
  config-single-source posture — routing keys off declared capability, not hardcoded model names.
- A reusable foundation (carrier, credentialed fetch, content widening, capability routing) that
  ADR-0102 (documents) builds on directly.
- No new data-egress boundary: the attachment travels with the profile the user already chose.

### Negative Consequences

- **Content type widening — a non-trivial blast radius.** `Message.content` (`service/models.py:130`)
  moves from `str` to `str | list[<block>]`, and **`LLMResponse.content` (`llm_client/types.py:81`)
  remains `str`** — the widening is inbound-only and the audit must confirm no inbound path assumes
  `str`. `history_sanitiser.py:259` already type-guards, but a review pass surfaced multiple sites
  that stringify or assume `str` content and would silently corrupt or drop a block list. The audit
  must cover **all** content-touching sites; the known set so far:
  `_validate_and_fix_conversation_roles` (`executor.py:633`) and the debug log
  (`executor.py:2731-2740`); the no-think suffix injection (`executor.py:806-808`); frozen
  volatile-context inlining (`executor.py:885-887`); the expansion-query content read that collapses
  to `""` (`executor.py:1857`); duplicate-role merge via string interpolation
  (`executor.py:716-719`); and token estimation that stringifies rather than counting image tokens
  (`context_window.py:30-32`). This is a dedicated audit-and-harden ticket, not a three-line fix.
- **Cost.** Image blocks consume vision tokens; the cost gate (ADR-0065) must meter image blocks. The
  per-image resolution cap bounds this (no per-page multiplication — that risk lives in ADR-0102).
- **Routing reads attachment metadata.** `ExecutionContext` gains an attachment field and the routing
  seam now branches on it (previously a pure no-op returning `PRIMARY`).

### Risks and Mitigations

| Risk | Severity | Mitigation |
|------|----------|------------|
| A non-vision model silently receives an image and hallucinates | High | Routing asserts `supports_vision`; on failure escalate or raise `AttachmentUnsupported` — fail-closed, never silent (AC-4) |
| `content` widening breaks str-assuming code paths | Medium-High | Dedicated audit ticket over **all** content-stringifying sites (the known set: `history_sanitiser`, `_validate_and_fix_conversation_roles`, debug log, no-think suffix, frozen-context inlining, expansion-query read, duplicate-role merge, token estimation); add a single block-aware text accessor used everywhere; tests over assembled `request_messages` (AC-3) and over each audited site with list content |
| Oversized image blows context or cost | Medium | Per-image size/dimension cap + downscale, max images/turn, total payload cap, fail-closed guard (AC-7) |
| Declared `content_type` is wrong (no server-side magic-byte sniff today) | Low | Resolution validates the declared type against the allowlist before constructing a block; mismatched/unsupported types are **rejected with a clear, user-visible error** (fail-closed) — never silently downgraded to the text-pointer behavior |
| Local SLM rejects `image_url` blocks for a given model build | Low | Capability flag reflects the deployed build; if a profile's model is not actually vision-capable, set its flag false and routing escalates/raises rather than failing opaquely |

---

## Implementation Notes

**Files affected:**

- `orchestrator/orchestrator.py` — add `attachments` param to `handle_user_request`; build it into
  `ExecutionContext`.
- `orchestrator/executor.py` — `ExecutionContext` attachment field; turn-assembly injection at the
  `ctx.messages.append({"role":"user", ...})` site (`executor.py:1741`); routing seam at
  `_determine_initial_model_role` (`executor.py:1309`); **all** str-assuming content sites made
  block-aware via a shared accessor — the full set is enumerated under Negative Consequences
  (`_validate_and_fix_conversation_roles`, duplicate-role merge, no-think suffix, frozen-context
  inlining, expansion-query read, debug log, plus `context_window.py` token estimation), not just the
  two obvious ones.
- `service/app.py` — pass the validated structured attachment list (not the augmented string); retire
  `_augment_message_with_attachments` from the orchestrator path.
- `service/models.py` — widen `Message.content` to `str | list[<block>]`.
- `tools/artifact_tools.py` — binary-path honesty (stop advertising `public_url` as agent-fetchable).
- `llm_client/models.py` — `ModelDefinition.supports_vision`.
- `config/models.yaml` — set `supports_vision` on `primary`, `sub_agent`, `claude_sonnet`,
  `claude_haiku`.
- New attachment-resolution module — `store.get` fetch + image block construction + guardrails
  (downscale/caps). Designed so ADR-0102 can add a document branch without reshaping it.

**Dependencies:** R2 store (ADR-0069), the structured-attachment carrier (folded FRE-661).

**Testing strategy:** unit tests over the resolution module (byte fetch path, block shape, guardrails)
with a mocked `store`; an assertion over assembled `request_messages` for block presence; a routing
test for the capability assertion; a master live smoke for the end-to-end "agent sees the image"
outcome.

---

## Verification / Acceptance Criteria

**How will we know this decision actually delivered — not just merged?**

- **AC-1 (agent sees the image)** — A `/chat` turn with an image attachment produces a response
  demonstrably conditioned on image *content*. **Check:** a live turn with a test image containing a
  unique visual marker (e.g., a specific word or object visible only in the image); assert the
  response references that marker. *Fails if* the response is generic or says it cannot see the image.
- **AC-2 (credentialed fetch, never CF Access)** — The image bytes reaching the model came from
  `store.get(r2_key)`, not a `public_url` HTTP fetch. **Check:** an integration test asserts
  `store.get` is called with the artifact's `r2_key` and that no agent-side code issues an HTTP GET to
  `artifacts_public_base_url` during resolution. *Fails if* any agent-side path fetches the CF-Access
  URL for bytes.
- **AC-3 (typed image block in the initial message)** — The first model call's message list contains a
  typed image block for the attachment — not a text pointer, not base64 in a text field. **Check:**
  assert on the assembled `request_messages` that the user turn's `content` is a list containing a
  block of type `image_url`/`image`. *Fails if* `content` is a `str` or the image is embedded as text.
- **AC-4 (routing guarantees vision capability)** — When an image attachment is present, the model that
  receives it has `supports_vision=true`. **Check:** a test that, given a profile whose primary has
  `supports_vision=false`, the router either escalates to a vision-capable model or raises
  `AttachmentUnsupported` — and a non-vision model never receives an image block. *Fails if* a model
  with `supports_vision=false` is handed an image block.
- **AC-5 (clean task description — FRE-661 folded)** — For an attachment turn, the captured task text
  equals the user's original submitted message **byte-for-byte**, and attachment metadata appears
  **only** in the structured attachment carrier field. **Check:** assert `TaskCapture.user_message`
  (`captains_log/capture.py`) and any persisted task description are byte-for-byte equal to the
  original submitted user text; assert no artifact ID, content-type, filename, or stringified block
  appears in that field, and that attachment metadata is present in the structured carrier. *Fails if*
  the captured text differs from the original by any byte (preamble, appended IDs, or a stringified
  block list) — not merely if the exact `"[Attachments —"` preamble is present.
- **AC-7 (every guardrail dimension fails closed)** — For **each** configured cap — per-image byte
  size, per-image pixel dimension, images-per-turn, and total per-turn payload — an over-limit input
  is either transformed below the limit before the model call or rejected with a user-visible error;
  the over-limit bytes never reach the model. **Check:** one parametrized test per cap dimension feeds
  an input exceeding that specific cap and asserts (a) the resolved block is below the cap or the turn
  is rejected, and (b) the over-limit content is absent from the assembled `request_messages`. *Fails
  if* any single dimension (e.g. the multi-image total) passes through unbounded while another is
  enforced.
- **AC-8 (artifact_read honesty)** — For a binary/image artifact, `artifact_read` exposes no
  agent-readable field that presents a URL as a byte/content-access path. **Check:** call
  `artifact_read` on an image artifact and assert that any URL in the result is **either absent from
  the agent-readable content or explicitly marked human-display-only** (e.g. a `human_display_url`
  key, not a bare `public_url`), and that no returned field describes the URL as a way to read/fetch
  the bytes. *Fails if* a bare `public_url` (or any field) remains that an agent would reasonably read
  as a fetchable content source — the absence of the word "fetch" is not enough.

**Seam owner (decomposed ADR):** the assembled intent is **AC-1 + AC-3 + AC-4 together** — structured
image attachment → resolved image block → vision-capable model → response conditioned on the image,
end to end. This seam is owned by the **final image live-smoke ticket (AC-1)**, run by master at the
integration gate; the chain does **not** close because the routing-flag or carrier ticket merged in
isolation.

---

## References

- ADR-0069 — R2 artifact substrate (the `store.get` credentialed byte path)
- ADR-0070 — output channels (human-facing `public_url` display)
- ADR-0099 — configuration management & validation (config-single-source for capability flags)
- ADR-0033 — model role taxonomy (`ModelRole.PRIMARY`; routing seam)
- ADR-0065 — cost gate (must meter vision tokens)
- ADR-0102 — Document Ingestion (forthcoming) — PDF/OCR/chunking; reuses this ADR's foundation
- FRE-369 — upload UX, live (surfaced this gap)
- FRE-368 — agent-side artifact tools (`artifact_read` origin)
- FRE-661 — structured attachments through `handle_user_request` (folded into this ADR's chain)
- FRE-662 — this ADR's tracking issue
- Code anchors — `service/app.py:187-211`, `tools/artifact_tools.py:672-690`,
  `storage/artifact_store.py:222-253`, `orchestrator/orchestrator.py:38`, `executor.py:1309,1741`,
  `service/models.py:130`, `litellm_client.py:57-61`, `config/models.yaml`

---

## Status Updates

### 2026-06-28 - Proposed
**Changed By:** lextra (adr session, Opus)
**Reason:** Design pass for FRE-662. Turn-assembly resolution + capability-driven routing; folds
FRE-661 as the structured-attachment carrier; scope raster + PDF, current-turn.

### 2026-06-29 - Scope narrowed to images
**Changed By:** lextra (adr session, Opus)
**Reason:** Owner call — documents (PDF) merit their own ADR. An image is one bounded visual block
(no pagination/chunking/OCR); documents carry a divergent decision shape (page count, scanned-vs-text
layer, OCR-vs-extraction, chunking, per-page cost). Narrowed this ADR to raster images; carved
documents into **ADR-0102 (forthcoming)**, which reuses this ADR's foundation. Still Proposed.

---

**Template Version:** 1.1
**Based On:** [Michael Nygard's ADR pattern](https://cognitect.com/blog/2011/11/15/documenting-architecture-decisions)
