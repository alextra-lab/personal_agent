# ADR-0101: Agent Vision Ingestion of Uploaded Images

**Status:** Accepted
**Date:** 2026-06-28
**Deciders:** lextra (owner), Seshat architecture
**Tags:** uploads, vision, images, model-routing, orchestrator, r2, cost, joinability, execution-profile

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
   `public_url` (`artifact_tools.py:267-271`) is the `artifacts.example.com/{id}` route, which is
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
- **Does an attachment carry a per-attachment cloud/local override**, and how does it interact with the
  profile's data-egress boundary? (Folded in by the FRE-690 amendment — see below.)
- **How is cloud vision cost bounded *before* the spend**, not just metered after it?
- **How do an attachment turn's cost and resolution events stay joinable to the turn** (no orphans)?

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

**Grounding facts for the FRE-690 control-spine amendment (verified against config + the ADR-0102
design pass — 2026-06-29):**

- **Cloud vs local is the ExecutionProfile (ADR-0044).** A conversation is bound to a profile at
  creation; an async context var (`config/profile.py:_current_profile`) carries it through the call
  chain. There is **no per-attachment path control today** — this amendment adds one (owner-requested),
  defined attachment-type-agnostically so ADR-0102 inherits it.
- **Cost is gated and reconciled (ADR-0065).** The cost gate reserves against `cap_usd` caps and
  commits actual cost via `litellm.completion_cost()` (`cost_gate/gate.py`). Cloud Claude pricing lives
  in the cost matrix (`config/models.cloud.yaml`); an attachment turn needs a **pre-flight estimate** to
  reserve *before* spending — a blank reservation would let an unbounded multi-image turn through. If
  cloud vision pricing is absent, image tokens meter as $0 (silent under-billing).
- **Joinability is an established pattern (ADR-0074).** `artifact_store.get` already threads `trace_id`
  (`storage/artifact_store.py:222`), R2 keys embed `session_id`, and `TraceContext` carries
  `trace_id`+`session_id`. The attachment path must thread `trace_id`/`session_id`/`task_id` onto its
  resolution and cost events so they join back to the turn with no orphans.
- **This control set was missing from the original image scope — owner-flagged.** ADR-0102 (documents,
  Accepted) added per-attachment override, pre-flight cost estimate + threshold confirmation, and
  joinability in its §7; the FRE-690 amendment folds the **same** control set into this ADR
  **attachment-type-agnostically** (§8 below) so the mechanisms are built **once** here and the ADR-0102
  cost/joinability tickets reuse them rather than rebuild them. An image is the bounded instance (one
  block, ≈1600 tokens max after resize, no page multiplication); the reserve/confirm/meter and
  joinability machinery is identical and shared.

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
`{artifact_id, content_type, title, r2_key}` **plus the optional `processing_target` path-control field
defined in §8a**. `service/app.py` passes the validated structured list
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
Accepted)**. ADR-0102 reuses this ADR's structured-attachment carrier (§2), credentialed fetch
(§3), turn-assembly injection, capability routing (§5), and the §8 shared control spine; it owns only
the document-strategy layer.

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

When the attachment carries a `processing_target` (§8a), it constrains routing **ahead of** the profile
default: `"local"` forbids the cloud-escalation branch above (fail-closed to `AttachmentUnsupported`
instead of crossing the boundary), and `"cloud"` forces the cloud vision path and is cost-gated (§8b).

`ExecutionContext` gains the attachment metadata so the routing seam (`_determine_initial_model_role`
/ its successor) can read it. The text-pointer augmentation is **not** retained as a degraded fallback
(that would silently reproduce the FRE-369 failure); an unsupported/mismatched attachment is rejected
explicitly, not quietly downgraded.

### 6. Guardrails (fail-closed, per dimension)

Four caps are enforced server-side at resolution, **each independently fail-closed** — no single
dimension may pass through unbounded while another is enforced:

- **per-image pixel dimension** — an over-limit image is downscaled (Pillow) below the cap before encoding;
- **per-image byte size** — after downscale/encode, an image still over the byte cap is rejected;
- **max images per turn** — beyond the cap the turn is rejected (or excess images dropped *with
  disclosure*), never silently truncated;
- **total per-turn attachment payload** — the aggregate across all images is capped independently of the
  per-image caps.

An over-limit input on any dimension is transformed below the cap (downscale) or rejected with a clear,
user-visible error; when content is altered (downscaled) or dropped (excess images) the change is
**disclosed** in the response — never sent unbounded, never silently dropped. (Document-specific
guardrails — page caps, extracted-text caps, chunking — belong to ADR-0102.)

### 7. Fix the broken `public_url` path

`artifact_read`'s binary path stops presenting `public_url` as a fetchable content source for the
agent (the agent cannot pass CF Access). For image artifacts it states the bytes are delivered via the
turn's content block (current-turn attachments) and are not URL-fetchable by the agent. `public_url`
remains for **human / output-channel display** (PWA, ADR-0070 rich output) only.

### 8. Shared control spine — per-attachment path control, cost, joinability (attachment-type-agnostic)

This ADR no longer carries only image-specific routing: it defines the **attachment-type-agnostic
control spine** — per-attachment path control, cost reservation/metering, and joinability — that
**ADR-0102 (documents) inherits rather than rebuilds** (the FRE-690 amendment). The mechanisms below
live on the shared `AttachmentRef` carrier (§2) and the shared resolution path; the image case is the
bounded instance (one block, ≈1600 tokens max after the §6 resize, no page multiplication), and the
PDF case (ADR-0102) reuses the same machinery with page-multiplied cost. Building the spine here, once,
is the explicit payoff of sequencing the image chain before the ADR-0102 cost/joinability tickets.

**8a. Per-attachment cloud/local override.** `AttachmentRef` carries an optional
`processing_target: Literal["cloud", "local"] | None`:

- `None` (default) → follow the conversation's bound ExecutionProfile (ADR-0044): a local profile
  resolves and routes the image to the local vision-capable SLM; a cloud profile routes to cloud
  Claude. No user action required.
- `"local"` → force local handling. It **never escalates to cloud**, even on a profile that permits
  escalation — an attachment the user marked local never crosses the data-egress boundary. If no
  reachable model on the local profile is vision-capable, it fails closed with `AttachmentUnsupported`
  (§5), never a silent escalation.
- `"cloud"` → force the cloud vision path. This is the **only** way a local-profile conversation sends
  an image to cloud; it is explicit and **still subject to the cost gate** (§8b).

The override is the per-attachment refinement of §5's profile-driven routing: §5 decides capability and
escalation, while `processing_target` constrains *which side of the boundary* is even eligible and is
honored ahead of the profile default. The PWA exposes the override per attachment; the default (`None`)
requires no user action.

**8b. Cost — cloud pricing + pre-flight estimate + threshold confirmation + metering.** Cloud Claude
model definitions carry per-token pricing in the cost matrix (`config/models.cloud.yaml`) such that
image tokens produce **non-zero committed cost** — whether the provider bills them as ordinary input
tokens or via a distinct image-token field (a zero-cost placeholder does not satisfy this; see AC-11).
Before any cloud image call, resolution computes a **pre-flight
cost estimate** (image blocks × per-image vision-token estimate × cloud price) and **reserves it against
the ADR-0065 cost gate before spending** — a blank reservation would let an unbounded multi-image turn
through. Then:

- estimate ≤ configured threshold → proceed and meter.
- estimate > threshold → the agent **discloses the estimate and asks the user to proceed** (or keep it
  local/free), mirroring the §6 disclose-on-alter pattern. No spend until confirmed.

Actual cost is reconciled at commit via `litellm.completion_cost()`, whose token basis includes the
image tokens (not text-only). Local turns are metered too (zero per-token charge, recorded for
budget/observability consistency). A single image is cheap (≈1600 tokens), so threshold confirmation
**rarely fires for one image** — its value is (1) bounding a **multi-image** turn and (2) building the
**shared reserve/confirm/meter machinery** that ADR-0102's far more expensive PDF path (native block
≈7× text, page-multiplied) depends on. The image path is where this machinery is proven cheaply first.

**8c. Joinability (ADR-0074).** The resolution path threads `trace_id`, `session_id`, and `task_id`
onto the `store.get` byte fetch, the cost-gate reservation/commit, and every resolution / routing
telemetry event. An attachment turn's cost row and resolution events **join back to the turn** via
`(trace_id, session_id, task_id)` — verified by the ADR-0074 joinability probe with zero orphans.
`artifact_store.get` already threads `trace_id`; this extends the same identity threading to the cost
and resolution events the attachment path adds.

### Scope (v1)

Raster `image/png|jpeg|gif|webp`, **current-turn attachments only**, plus the shared control spine (§8):
per-attachment cloud/local override (fail-closed on `"local"`), cloud vision pricing + pre-flight cost
estimate with threshold confirmation reserved against the ADR-0065 cap, and trace/session/task
joinability on cost + resolution events. Out of scope: **documents (PDF) —
ADR-0102 (Accepted)**; SVG (XML, not a raster — Anthropic does not accept it as an image);
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

### Option 6: Profile-only routing, meter-after (no per-attachment override, no pre-flight reserve)

*(Alternative for the §8 control-spine decision.)*

**Description:** Route purely by the bound ExecutionProfile; carry no `processing_target`; meter cloud
cost only *after* the call (the existing cost-gate commit), with no pre-flight estimate or reservation.

**Pros:**
- Least surface — no carrier field, no PWA affordance, no estimator; the profile already encodes the
  cloud/local choice.

**Cons:**
- Gives the user no way to keep a *specific* attachment local on a cloud-escalating profile, nor to
  deliberately send one image to cloud from a local-profile conversation — the boundary is all-or-nothing.
- Meter-after cannot stop an expensive turn (a multi-image turn, or a PDF on the ADR-0102 path) *before*
  it spends; the `cap_usd` ceiling is the only backstop, and it rejects mid-turn rather than
  disclosing-and-confirming, so the user is surprised by the spend rather than asked about it.

**Why Rejected:** The owner wants per-attachment control over the data-egress boundary and a
spend-before-confirm gate; the document path (ADR-0102, ≈7× cost) makes pre-flight reservation
load-bearing, not cosmetic. Meter-after-only here would force a rebuild for documents.

### Option 7: Build the control spine only in ADR-0102 (PDF), not shared

*(Alternative for the §8 control-spine decision.)*

**Description:** Leave the image scope as-is (no override, cost confirmation, or joinability) and build
the path-control + cost + joinability machinery solely in the ADR-0102 document chain.

**Pros:**
- Keeps the image ADR minimal; the controls land where the expensive case (PDF) actually lives.

**Cons:**
- The image chain lands **first** — it is the dependency ADR-0102 builds on — so building the spine in
  ADR-0102 means the image path ships *without* override/cost-confirm/joinability and ADR-0102 then
  retrofits them onto the shared carrier and resolution path the image chain already created: the same
  machinery built **twice**, re-touching the image path. It also leaves cloud image turns
  unmetered-before-spend and image cost/resolution events orphaned in the interim.

**Why Rejected:** FRE-690's sequencing rationale — the image chain is the foundation; folding the
attachment-type-agnostic spine in here builds it **once**, and ADR-0102's cost and joinability tickets
become reuse-plus-PDF-specifics rather than fresh builds. That is the payoff of sequencing ADR-0101
first.

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
- **Per-attachment path control:** the user can pin an attachment local (never crosses the egress
  boundary) or deliberately send one to cloud — finer-grained than the profile alone, fail-closed on
  `"local"`.
- **Cloud cost is controlled before the spend:** cloud image turns are estimated, reserved against the
  cap, and confirmed past a threshold — no surprise spend — and the machinery is shared with the
  expensive PDF path.
- **Cost and resolution events are joinable** (trace/session/task), attributable end-to-end via the
  existing ADR-0074 probe.
- **The control spine is attachment-type-agnostic and built once:** ADR-0102 inherits it for PDFs rather
  than rebuilding — the cost and joinability tickets become reuse-plus-PDF-specifics.

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
- **More control surface (§8).** A `processing_target` field on the carrier (+ a PWA affordance), a
  pre-flight cost estimator, a configurable cost threshold, and trace/session/task threading on the
  attachment path — each is small, but together they widen the change beyond the bare image-resolution
  logic.
- **Pre-flight estimate is approximate.** The reservation uses a per-image token estimate; actual cost
  is reconciled at commit, so the reservation may over- or under-shoot. Bounded by reconciliation and
  the hard `cap_usd` ceiling.
- **Threshold confirmation rarely fires for a single image** (≈1600 tokens) — the machinery is justified
  by multi-image turns and by the shared PDF path, not by the single-image case alone. Accepted: the
  value is building the spine once.

### Risks and Mitigations

| Risk | Severity | Mitigation |
|------|----------|------------|
| A non-vision model silently receives an image and hallucinates | High | Routing asserts `supports_vision`; on failure escalate or raise `AttachmentUnsupported` — fail-closed, never silent (AC-4) |
| `content` widening breaks str-assuming code paths | Medium-High | Dedicated audit ticket over **all** content-stringifying sites (the known set: `history_sanitiser`, `_validate_and_fix_conversation_roles`, debug log, no-think suffix, frozen-context inlining, expansion-query read, duplicate-role merge, token estimation); add a single block-aware text accessor used everywhere; tests over assembled `request_messages` (AC-3) and over each audited site with list content |
| Oversized image blows context or cost | Medium | Per-image size/dimension cap + downscale, max images/turn, total payload cap, fail-closed guard (AC-7) |
| Declared `content_type` is wrong (no server-side magic-byte sniff today) | Low | Resolution validates the declared type against the allowlist before constructing a block; mismatched/unsupported types are **rejected with a clear, user-visible error** (fail-closed) — never silently downgraded to the text-pointer behavior |
| Local SLM rejects `image_url` blocks for a given model build | Low | Capability flag reflects the deployed build; if a profile's model is not actually vision-capable, set its flag false and routing escalates/raises rather than failing opaquely |
| A `"local"` override is silently escalated to cloud (boundary breach) | High | Override honored strictly: `"local"` never escalates — fail-closed to `AttachmentUnsupported` instead (AC-9) |
| A `"cloud"` override (or cloud profile) runs up cost on a multi-image turn | Medium | Pre-flight estimate + reservation against `cap_usd`; threshold confirmation before spend (AC-10); actual reconciled at commit (AC-11) |
| Cloud vision pricing missing → image tokens metered as $0 (silent under-billing) | Medium | Cloud vision pricing asserted present in the cost matrix; non-zero committed cost with image-token basis asserted (AC-11) |
| Image cost/resolution events orphaned (not joinable to the turn) | Medium | Thread `trace_id`/`session_id`/`task_id` through resolution + cost; ADR-0074 probe asserts zero orphans (AC-12) |

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
- `AttachmentRef` carrier — add `processing_target: Literal["cloud","local"] | None` (§8a); orchestrator
  routing reads it ahead of the profile default and enforces the `"local"` no-escalation rule.
- Cost gate (`cost_gate/`) — pre-flight image cost estimator (image blocks × per-image vision tokens ×
  price) → reservation; meter image tokens at commit. Built attachment-type-agnostically so the ADR-0102
  document cost ticket reuses it.
- `config/models.cloud.yaml` — ensure cloud Claude per-token vision pricing is present; `config/` — the
  cost-confirmation threshold (ADR-0099 config-single-source).
- Attachment path telemetry — thread `trace_id`/`session_id`/`task_id` onto resolution, routing, and
  cost events (ADR-0074 joinability). Shared with the ADR-0102 document joinability ticket.
- PWA (`seshat-pwa/`) — per-attachment cloud/local override affordance (defaults to none).

**Dependencies:** R2 store (ADR-0069), the structured-attachment carrier (folded FRE-661); the cost gate
(ADR-0065) and execution profiles (ADR-0044) for the §8 control spine.

**Testing strategy:** unit tests over the resolution module (byte fetch path, block shape, guardrails)
with a mocked `store`; an assertion over assembled `request_messages` for block presence; a routing
test for the capability assertion **and the `processing_target` override (local fail-closed / cloud
forced + cost-gated)**; a cost test asserting a pre-flight reservation precedes a cloud call, an
over-threshold turn is held until confirmation, and the committed cost is non-zero with an image-token
basis; an ADR-0074 joinability assertion (zero orphans on the cost + resolution events); a master live
smoke for the end-to-end "agent sees the image" outcome **plus the override/cost/joinability seam legs**.

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
- **AC-9 (per-attachment override honored, fail-closed)** — A `"local"` `processing_target` never
  reaches cloud even on an escalation-permitted profile; a `"cloud"` override routes to the cloud vision
  path and is cost-gated. **Check:** test (a) `processing_target="local"` on a cloud-escalation-enabled
  profile whose local model is non-vision, with an image → assert **no** cloud call is made and
  `AttachmentUnsupported` is raised (not a silent escalation); test (b) `processing_target="cloud"` from
  a local-profile conversation → assert the cloud vision path is taken **and** a cost-gate reservation
  was made. *Fails if* a `"local"`-marked image reaches cloud, or a `"cloud"`-marked image bypasses the
  cost gate.
- **AC-10 (pre-flight estimate gates spend, and confirm actually proceeds)** — A cloud image turn whose
  estimated cost exceeds the threshold does **not** call the model until the user confirms; on
  confirmation it **does** proceed; an under-threshold turn proceeds directly with a reservation recorded
  *before* the call. **Check:** (a) feed a cloud image turn whose estimate exceeds the threshold (e.g.
  several images, or a deliberately low test threshold) → assert **no** model call is issued and the
  response carries the dollar estimate + a proceed/keep-local prompt; (b) supply the confirmation →
  assert the model **is** then called and the spend is committed; (c) feed an under-threshold turn →
  assert it proceeds and a cost-gate reservation ≈ the estimate is recorded *before* the call. *Fails
  if* an over-threshold cloud turn calls the model without disclosure, no reservation precedes the
  spend, **or** a confirmed turn never proceeds (a dead-end prompt).
- **AC-11 (cloud images are priced and metered, not free)** — Cloud Claude vision pricing is present in
  the cost matrix and the committed cost is non-zero, reconciled from actual usage, and includes the
  image-token component. **Check:** assert the cloud model definition in `config/models.cloud.yaml`
  carries per-token pricing; for a cloud image turn assert the cost-gate `commit` records a non-zero
  `actual_cost` from `litellm.completion_cost()` whose token basis includes image tokens (not
  text-only). *Fails if* cloud vision pricing is missing (metered as $0), the spend is never committed,
  or an image turn is metered as text-only.
- **AC-12 (joinability — no orphan cost/resolution rows)** — An image turn's cost row and resolution
  events carry `trace_id` + `session_id` + `task_id` and join back to the turn. **Check:** after an
  image turn, run the ADR-0074 joinability probe (`observability/` joinability probe) and assert the
  cost-gate row and the resolution/routing telemetry join to the turn's
  `(trace_id, session_id, task_id)` with **zero** orphans. *Fails if* any image cost or resolution event
  lacks a join key or is orphaned per the probe.

**Seam owner (decomposed ADR) — AC-SEAM.** The assembled intent holds only once every child lands; it
is owned by the **final image live-smoke ticket (FRE-669)**, run by master at the integration gate. The
chain does **not** close because the routing-flag, carrier, cost, or joinability ticket merged in
isolation.

- **AC-SEAM (end-to-end, the whole image pipeline in one live run)** — In a single live session: (1) a
  structured image attachment becomes a resolved **image block**, routes to a **vision-capable** model,
  and returns a response conditioned on the image's visual marker (AC-1 + AC-3 + AC-4); (2) the same
  image under a `"local"` override on an escalation-permitted profile with no capable local model
  **fails closed** with `AttachmentUnsupported`, no cloud crossing (AC-9); (3) a cloud image turn
  **whose pre-flight estimate exceeds the threshold** is held until confirmation, then on confirmation
  proceeds and **commits a non-zero cost whose token basis includes image tokens** (AC-10 + AC-11); and
  (4) the ADR-0074 joinability probe reports **zero orphans** for those turns (AC-12). **Check:** master
  runs all four legs against the live stack. *Fails if* any leg regresses — which it will unless the
  carrier (with `processing_target`), content widening, capability routing, guardrails, the cost gate
  (estimate + confirm + meter), and joinability threading have **all** landed. The ADR does **not** close
  because the last child ticket merged in isolation — only AC-SEAM closes it.

---

## References

- ADR-0069 — R2 artifact substrate (the `store.get` credentialed byte path)
- ADR-0070 — output channels (human-facing `public_url` display)
- ADR-0099 — configuration management & validation (config-single-source for capability flags + cost threshold)
- ADR-0033 — model role taxonomy (`ModelRole.PRIMARY`; routing seam)
- ADR-0065 — cost gate (pre-flight reservation + commit; must meter vision/image tokens)
- ADR-0044 — execution profiles (cloud/local binding; the §8a per-attachment override defaults to it)
- ADR-0074 — joinability (trace/session/task threading; the probe that asserts no orphans)
- ADR-0102 — Document Ingestion (Accepted) — PDF/OCR/chunking; reuses this ADR's foundation **and the §8 shared control spine**
- FRE-369 — upload UX, live (surfaced this gap)
- FRE-368 — agent-side artifact tools (`artifact_read` origin)
- FRE-661 — structured attachments through `handle_user_request` (folded into this ADR's chain; amended to add `processing_target`)
- FRE-662 — this ADR's tracking issue
- FRE-690 — this amendment's authoring issue (the shared control spine, §8)
- Code anchors — `service/app.py:187-211`, `tools/artifact_tools.py:672-690`,
  `storage/artifact_store.py:222-253`, `orchestrator/orchestrator.py:38`, `executor.py:1309,1741`,
  `service/models.py:130`, `litellm_client.py:57-61`, `config/models.yaml`

---

## Status Updates

### 2026-07-15 - §8a Auto default changed to cloud (FRE-886)
**Changed By:** lextra (owner)
**Reason:** During the live AC-SEAM run, local Qwen produced a materially worse read of a scanned page
than cloud Sonnet. §8a's documented default ("`None` → follow the conversation's bound
ExecutionProfile") is superseded: Auto (no per-attachment override) now routes straight to the
profile's cloud escalation model by default, config-driven via
`attachment_default_processing_target` (`config/settings.py`, default `"cloud"`) so it can be flipped
back to the original profile-following behavior (`"local"`). The explicit `"local"`/`"cloud"`
per-attachment overrides are unchanged. Applies identically to ADR-0102 §7a (documents) via the same
setting.

### 2026-06-30 - Accepted
**Changed By:** lextra (owner; recorded by master)
**Reason:** Owner accepted ADR-0101 (images) and approved the underlying implementation chain. The image
chain FRE-661/664/665/666/668/669 was already Approved; the shared-control-spine tickets **FRE-691**
(cloud image cost + ADR-0065 gate) / **FRE-692** (PWA per-attachment override) / **FRE-693** (ADR-0074
joinability) are now Approved too. Design is complete: turn-assembly resolution + credentialed
`store.get` fetch + capability-routed fail-closed + §8 shared control spine. Build chain unblocked;
the ADR-0102 document chain (FRE-682+) hard-depends on this image chain landing first. Seam **FRE-669
(AC-SEAM)** owns the assembled intent — the ADR closes only when 669 passes all legs, not when the last
child merges.

### 2026-06-28 - Proposed
**Changed By:** lextra (adr session, Opus)
**Reason:** Design pass for FRE-662. Turn-assembly resolution + capability-driven routing; folds
FRE-661 as the structured-attachment carrier; scope raster + PDF, current-turn. *(Scope superseded by
the 2026-06-29 update below — narrowed to images.)*

### 2026-06-29 - Scope narrowed to images
**Changed By:** lextra (adr session, Opus)
**Reason:** Owner call — documents (PDF) merit their own ADR. An image is one bounded visual block
(no pagination/chunking/OCR); documents carry a divergent decision shape (page count, scanned-vs-text
layer, OCR-vs-extraction, chunking, per-page cost). Narrowed this ADR to raster images; carved
documents into **ADR-0102**, which reuses this ADR's foundation. Still Proposed.

### 2026-06-29 - Amended: shared control spine folded in (FRE-690)
**Changed By:** lextra (adr session, Opus)
**Reason:** Owner-flagged that the per-attachment path control, cost reservation/metering, and
joinability added to ADR-0102 §7 were missing from the image scope. Folded the **same** control set into
this ADR **attachment-type-agnostically** as the shared spine (§8) so it is built **once** and ADR-0102
inherits it: a `processing_target` cloud/local override on the carrier (fail-closed on `"local"`, the
only cloud crossing via `"cloud"`, cost-gated); cloud vision pricing + a pre-flight estimate reserved
against the ADR-0065 cap with threshold confirmation + commit-time metering; trace/session/task
joinability (ADR-0074) on cost + resolution events; and §6 guardrails made explicit + fail-closed per
dimension. Added AC-9–AC-12, extended the seam to AC-SEAM (override fail-closed + cost confirm/meter +
zero-orphan joinability), and added Alternatives 6–7 for the spine decision. The image chain is
re-ticketed to fold these in (FRE-661/665/666 amended; new image-cost, image-joinability, and
PWA-override tickets; FRE-669 seam extended); the image-cost and image-joinability tickets build the
spine generically, so ADR-0102's FRE-686 (cost) and FRE-688 (joinability) become reuse-plus-PDF-specifics.
**Status stays Proposed** (no implementation has landed).

---

**Template Version:** 1.1
**Based On:** [Michael Nygard's ADR pattern](https://cognitect.com/blog/2011/11/15/documenting-architecture-decisions)
