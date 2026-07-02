# ADR-0108: Stored-Artifact Vision Re-processing (analyze-to-text, explicit tool)

**Status:** Proposed
**Date:** 2026-07-02
**Deciders:** lextra (owner), Seshat architecture
**Tags:** uploads, vision, images, artifacts, sub-agent, r2, cost, joinability, tools

---

## Context

**What is the issue we're addressing?**

ADR-0101 (Accepted) delivered vision for **current-turn** image uploads: an image the user attaches
*this turn* is resolved into a typed content block at turn assembly and routed to a vision-capable
model. But an image the user references from an **earlier turn or session** — one already stored as an
`artifacts` row in R2 — cannot be seen at all.

The gap is grounded in a live trace (2026-07-02, session `dfa03ca9` / trace `ab8964ec`, on the
FRE-734-fixed gateway). The owner asked about `IMG_3075.jpeg` (uploaded June 28, artifact `75abb42f`,
~4.7 MB) **without re-attaching it**. FRE-666 resolves only current-turn raster attachments
(`ctx.attachments`), so the vision path never engaged. The agent instead ran
`recall_personal_history` → `artifact_read`, which for a binary/image artifact returns
`inline=False`, `content: None`, and a `public_url` (`artifacts.frenchforet.com/{id}`). That URL is
guarded by **Cloudflare Access**, so the agent gets a 403 login page — it cannot fetch the image and
cannot see it, even though the bytes sit in R2 and the credentialed `store.get(r2_key)` path (used by
FRE-666 and `artifact_read`'s textual branch) reads them directly.

Two coupled problems:

1. **No path to vision-ize a previously-stored image.** The agent can see an image attached *this*
   turn, but not one referenced from history. Studying a diagram or photo across sessions — a
   pedagogical use case (Seshat's North Star) — is impossible today.
2. **`artifact_read` hands the agent an unfetchable URL for binary artifacts.** For image/binary types
   it returns `public_url` with `content: None` and a docstring telling the agent to "direct the user
   to open the URL directly" (`tools/artifact_tools.py:604-607, 659-670`). The agent cannot pass CF
   Access, so this is a structural dead end — a URL the model can never GET.

**Grounding facts (verified against `main`, 2026-07-02):**

- The credentialed byte path exists and is already used for stored artifacts:
  `store.get(row.r2_key, trace_id=…)` (`storage/artifact_store.py`, called at
  `tools/artifact_tools.py:689`) — direct R2 S3, **bypasses Cloudflare Access**, returns raw bytes.
  `artifact_read`'s textual branch uses it; the binary branch does not.
- The image-resolution machinery is live: `orchestrator/attachment_resolution.py` (FRE-666) turns
  image bytes into a capped, downscaled OpenAI-style `image_url` block (`RASTER_CONTENT_TYPES`,
  `_downscale_if_needed`, per-image/per-turn caps, disclose-on-alter). It is keyed on the current
  turn's `AttachmentRef` list, **not** on a stored `artifact_id`.
- Capability-driven routing is live: `ModelDefinition.supports_vision` (FRE-665) with fail-closed
  behavior (`AttachmentUnsupportedError`) and no silent cross-boundary escalation.
- Cost gating is live: `orchestrator/attachment_cost.py` (FRE-691) is the pre-flight cloud image
  **estimator**; the reservation/commit of actual spend against the ADR-0065 cost gate lives in
  `LiteLLMClient` (the two are distinct — see §4).
- A sub-agent seam exists: `artifact_draft` (ADR-0077) obtains a role-pinned client via
  `get_llm_client(role_name="sub_agent")` and calls `sub_agent_client.respond(role=…)`
  (`tools/artifact_tools.py:1410-1443`); `orchestrator/sub_agent.run_sub_agent(spec)` pins a model by
  `spec.model_role`. A vision analysis is a single role-pinned `respond()` over an image block.
- `artifact_read` already enforces owner-scoping: `WHERE id = :artifact_id AND user_id = :user_id`
  (`tools/artifact_tools.py:637-641`) — cross-user reads return not-found.

**What needs to be decided:**

- **How does a stored image reach vision** — injected as a raw image block into the live conversation
  (the model must then be vision-capable, and the block is transient), or analyzed by a vision model
  that returns **text** into the turn (the main model need not be vision-capable, and the analysis
  persists cheaply)?
- **What triggers it** — an explicit tool the model reaches for, or an automatic route when the user
  references a stored image?
- **Authorization, eligibility, cost, and joinability** for a stored-artifact re-process — a
  re-process is a data-egress and cost event exactly like a fresh upload.
- **Where the `artifact_read` binary-URL defect (problem 2) is fixed.**
- **New ADR or an ADR-0101 amendment.**

ADR-0101 §5 (Alternatives, Option 1) explicitly **named this capability as a deferred v2**: the
tool-result image-block path it rejected *for uploads* it kept as "the future path for 'examine an
arbitrary stored image'." ADR-0108 is that v2 — with the mechanism decision re-opened in light of the
owner's persistence-economics argument (below).

---

## Decision

Add an **explicit tool the model calls** to see a stored image artifact. The **default** mechanism is
**analyze-to-text**: the tool fetches the bytes over the credentialed path, runs a **vision-capable
sub-agent** over them, and returns a **structured text analysis** into the turn — which persists as
ordinary tool/assistant text. A **secondary** tool delivers the raw image block for full in-turn
fidelity when the text analysis is insufficient. This is a **new ADR** (not an ADR-0101 amendment),
and it **reuses ADR-0101's shipped foundation** (credentialed fetch, resolution/guardrails, capability
routing, cost gate, joinability) rather than rebuilding it.

### 1. `analyze_artifact_image` — the default (analyze-to-text)

Tool signature: `analyze_artifact_image(artifact_id: str, question: str | None) -> {analysis: str, disclosures: [str]}`.

Flow, reusing shipped modules:

1. **Authorize** — look the artifact up with the existing owner-scoped query
   (`WHERE id = :artifact_id AND user_id = :user_id`). A non-owned or missing artifact returns a
   clear not-found error; **no bytes are fetched**.
2. **Eligibility** — accept only `RASTER_CONTENT_TYPES` (`image/png|jpeg|gif|webp`). PDFs (ADR-0102),
   SVG, text, and other types are rejected with a clear, actionable error — never fed to a vision
   model.
3. **Credentialed fetch** — `store.get(r2_key, trace_id=…)` (direct R2 S3, bypasses CF Access). The
   `public_url` is **never** used as a byte source.
4. **Guardrails** — reuse `attachment_resolution`'s per-image caps (pixel-dimension downscale, byte
   cap, disclose-on-alter). A single stored image, so images-per-turn / total-payload caps are
   trivially satisfied; the per-image caps apply and fail closed.
5. **Vision sub-agent** — build the `image_url` block and run **one role-pinned `respond()`** on a
   vision-capable model (ADR-0077 sub-agent seam), with the user's `question` (or a default "describe
   this image" instruction). The returned text is the analysis. The **main conversation model is never
   required to be vision-capable** — vision is delegated.
6. **Return text** — `{analysis, disclosures}`. The tool result is plain text; it persists in the
   conversation like any other tool output. The expensive multimodal payload stays **out** of the
   persistent window.

### 2. `view_artifact_as_image` — the secondary (inline block, full fidelity)

Tool signature: `view_artifact_as_image(artifact_id: str)`. Same authorize / eligibility /
credentialed-fetch / guardrails as §1, but instead of analyzing it delivers the **raw image block**
for the main model to see directly — the ADR-0101 §5 Option-1 path (image block inside `tool_result`).
This is the higher-fidelity path for follow-ups the analysis text did not anticipate ("what's in the
top-left corner?").

It is **gated and fail-closed** on a **distinct capability signal**, not on `supports_vision`. A model
can be vision-capable on the initial user message yet **not** accept image blocks inside `tool_result`
(the cloud Anthropic path does; a local OpenAI-compatible SLM build may not — the same uncertainty
ADR-0101 §5 flagged). So this path routes off a new `ModelDefinition.supports_tool_result_image: bool`
flag (config-single-source per ADR-0099), separate from `supports_vision`. When it is false the tool
**fails closed with a clear error that names `analyze_artifact_image` as the supported path** — it
never silently degrades or crosses the data-egress boundary. Delivering a typed image block in a tool
result may **also** require widening the tool-result message-content shape (today tool outputs
serialize to a string / `function_call_output.output`), which is a further reason this path is gated,
sequenced **last**, and deferrable if `supports_tool_result_image` cannot be asserted true on the
deployed model set.

### 3. Data-egress boundary — profile-governed, fail-closed local

A stored-image re-process is a data-egress event: a cloud vision sub-agent sends the bytes to
Anthropic. It follows the **conversation's bound ExecutionProfile (ADR-0044)** exactly as ADR-0101 §5
does — **no new boundary**:

- **Cloud profile** → the vision sub-agent runs on cloud Claude.
- **Local profile with a vision-capable local model** → runs locally; the bytes never leave the
  owner's infrastructure.
- **Local profile with no vision-capable local model** → **fails closed** with
  `AttachmentUnsupportedError`. There is **no implicit escalation to cloud** to service a stored-image
  re-process. (A per-call `processing_target` override is out of scope — the model calls a tool with an
  `artifact_id`, not an `AttachmentRef`; the profile is the boundary. A future ticket may add an
  explicit override if the owner wants per-call cloud opt-in from a local conversation.)

### 4. Cost — estimate + gated spend (reuse the FRE-691 estimator + the ADR-0065 gate)

The vision sub-agent call is a cost event, identical in shape to a fresh cloud upload, with **two
distinct mechanisms** the tool wires onto that call:

- **Pre-flight estimate + threshold confirmation (FRE-691 `attachment_cost`).** Compute the expected
  cloud image cost *before* the call; past the configured threshold the tool discloses the estimate and
  asks the user to proceed (or keep it local / decline), mirroring ADR-0101 §8b. A single image is
  cheap (≈1600 tokens) so this rarely fires — but the estimator is reused, not rebuilt.
- **Reservation + commit (ADR-0065 gate).** The sub-agent's model call spends through the cost gate
  exactly as sub-agent calls already do in `LiteLLMClient` — the estimate is reserved before the vision
  call and the actual cost (`litellm.completion_cost()`, image-token basis) is committed after. Local
  turns are metered at zero per-token charge for observability consistency.

The estimator (pre-flight dollar figure + confirmation) and the gate (reserve/commit of the actual
spend) are separate concerns; conflating them is the trap AC-6 guards against.

### 5. Joinability (ADR-0074)

Thread `trace_id` / `session_id` / `task_id` onto the credentialed fetch, the vision sub-agent call,
and the cost reservation/commit so a re-process turn's cost and vision events **join back to the turn**
with zero orphans (verified by the existing joinability probe). `store.get` already threads `trace_id`;
this extends the same identity threading to the sub-agent and cost events the tool adds.

### 6. Fix the `artifact_read` binary-URL defect (problem 2)

`artifact_read`'s binary/image branch stops presenting `public_url` as an agent-fetchable content
source. For an image artifact it (a) marks any URL as **human-display-only** (not a bare `public_url`
the agent reads as fetchable) and (b) **redirects the agent to `analyze_artifact_image`** as the way to
see the image. This mirrors ADR-0101 §7 / AC-8 (the current-turn honesty fix) and extends it to the
stored-read tool. The credentialed bytes are delivered by the vision tools (§1/§2), never by a URL the
agent hands itself.

### Scope (v1)

Raster image artifacts (`image/png|jpeg|gif|webp`) **owned by the calling user**. Ships:
`analyze_artifact_image` (default, analyze-to-text), the `artifact_read` binary-URL honesty +
redirect fix, cost gating, and joinability threading; `view_artifact_as_image` (secondary inline
block) is capability-gated and last/deferrable. **Out of scope:** documents/PDF (ADR-0102), SVG,
non-owned artifacts, audio/video, and a per-call cloud/local override for the tool (profile governs).

---

## Alternatives Considered

### Option 1: Inline image block as the primary (auto-resolve on reference)

**Description:** Make the raw image block the default — detect when the user references a stored image
and auto-inject the block into the turn (or make `view_artifact_as_image` the primary), requiring the
main model be vision-capable.

**Pros:**
- Highest fidelity — the model sees the actual pixels and can answer any follow-up in-turn.
- Reuses the FRE-666 resolution machinery most directly.

**Cons:**
- An automatic route is a rigid pipeline that fires even when vision isn't wanted — violates the
  FRE-727 no-cage principle the owner asked to honor.
- Forces the main conversation model to be vision-capable, and re-touches the `Message.content`
  widening blast radius (image blocks in persistent history) that ADR-0101's Negative Consequences
  enumerate.
- Nothing persists — the analysis is re-derived every time; a cross-session "study this diagram" flow
  re-sends the multi-MB image each turn.

**Why Rejected:** The owner's persistence-economics argument (ticket comment, 2026-07-02): the current
path already drops the image and only the *text* survives, so analyze-to-text makes that deliberate and
turns the analysis into durable knowledge. Inline block is kept as the **secondary, explicit** tool for
the fidelity case, not the default, and not auto-routed.

### Option 2: Overload `artifact_read` to return image bytes / an image block

**Description:** Extend `artifact_read` so its binary branch returns the image bytes (base64) or a
typed image block, rather than adding sibling tools.

**Pros:**
- One tool for all artifact reads; no new tool surface.

**Cons:**
- Base64 bytes in a text field do **not** deliver vision — a model sees an image only from a typed
  block (ADR-0101 Option 2). Returning a block from `artifact_read` inherits the uneven `tool_result`
  image-block support on local SLMs and conflates a metadata/text read with the vision path.
- Buries the analyze-vs-view decision inside a general read tool; the model can't self-select by tool
  name.

**Why Rejected:** Sibling tools with clear names (`analyze_artifact_image`, `view_artifact_as_image`)
are self-describing (the no-cage / self-describing preference) and keep `artifact_read` simple. Problem
2 is fixed by making `artifact_read` **honest and redirecting**, not by overloading it.

### Option 3: Mint an agent-fetchable (signed / time-limited) URL

**Description:** Instead of server-side byte fetch, give the agent a pre-signed R2 URL (or a
CF-Access-service-token URL) it can GET directly.

**Pros:**
- No change to the tool's byte-handling; the agent "just fetches."

**Cons:**
- A URL still does not let a model *see* an image — vision needs a typed block, not a fetched URL, so
  this solves nothing for the vision case.
- Minting agent-fetchable credentialed URLs is a new auth/exfiltration surface for zero benefit over
  `store.get`, which already returns the bytes server-side.

**Why Rejected:** The credentialed server-side path exists and is strictly safer; a URL the agent
fetches is both unnecessary and doesn't deliver vision.

### Option 4: Amend ADR-0101 instead of a new ADR

**Description:** Fold stored-image handling into ADR-0101 as a v2 amendment, since it reuses the same
spine.

**Pros:**
- One ADR for all image ingestion; shared foundation is documented in one place.

**Cons:**
- The decision shape genuinely differs — tool-driven (not turn-assembly), analyze-to-text default (not
  raw block), and its own authorization/persistence semantics. Folding it in buries a distinct
  decision.
- ADR-0101 is **Accepted** with a live, partially-shipped build chain (FRE-661/664–669, 691–693) and
  a seam ticket (FRE-669). Reopening it to graft a new default mechanism muddies that seam and its
  acceptance gate.

**Why Rejected:** A new ADR keeps ADR-0101's Accepted seam clean and gives the tool-driven,
analyze-to-text decision its own record and acceptance criteria. ADR-0108 **references** ADR-0101's
foundation and reuses its modules; it does not re-decide them.

---

## Consequences

### Positive Consequences

- The agent can finally see a **previously-stored** image — closing the cross-session gap the live
  trace exposed, and enabling the pedagogical "study this diagram over time" flow.
- The default analyze-to-text path makes the **analysis durable** (persists as text) while keeping the
  expensive multimodal payload out of the persistent window — deliberate, not accidental.
- The **main conversation model need not be vision-capable** — vision is delegated to a sub-agent — and
  the `Message.content` widening blast radius is **not** re-touched (the tool result is plain text).
- Problem 2 is fixed: `artifact_read` stops handing the agent a URL it structurally cannot fetch and
  points it at the working vision tool.
- Reuses ADR-0101's shipped foundation (credentialed fetch, guardrails, capability routing, cost gate,
  joinability) — the new surface is a tool wrapper, not a new spine.
- No new data-egress boundary — the re-process follows the profile the user already chose, fail-closed
  on a local profile with no capable local model.

### Negative Consequences

- **Analyze-to-text is lossy** for unanticipated follow-ups — a question the sub-agent's analysis
  didn't cover needs a re-run (a second vision call / cost event). Mitigated by the secondary
  `view_artifact_as_image` tool and by letting the model pass a specific `question`.
- **A second vision hop.** The main model calls a tool that calls a vision model — one extra model call
  and its latency/cost per re-process. Bounded by the per-image cost (≈1600 tokens) and the cost gate.
- **New tool surface.** Two tools (one built first, one gated/deferred) plus governance entries and the
  `artifact_read` change — more than a one-line fix, though each piece reuses an existing module.
- **`view_artifact_as_image` depends on uneven `tool_result` image-block support** and may require
  widening the tool-result message-content shape (tool outputs serialize to a string today) — hence it
  is gated on a distinct `supports_tool_result_image` signal, fail-closed, sequenced last, and
  deferrable.

### Risks and Mitigations

| Risk | Severity | Mitigation |
|------|----------|------------|
| A stored image is analyzed for a user who does not own it (cross-user leak) | High | Reuse the owner-scoped `WHERE user_id = :user_id` query; no bytes fetched for a non-owned/missing artifact (AC-3) |
| A non-image artifact (PDF/text) is fed to the vision model | Medium | Eligibility restricted to `RASTER_CONTENT_TYPES`; other types rejected before any fetch/vision call (AC-4) |
| A local-profile image is silently sent to cloud vision | High | Profile governs; local profile with no vision-capable local model fails closed (`AttachmentUnsupportedError`), never escalates (AC-5) |
| The agent is handed a CF-Access URL it cannot fetch (problem 2 persists) | Medium | `artifact_read` binary branch marks URLs human-display-only and redirects to `analyze_artifact_image` (AC-9) |
| A raw image block leaks into persistent history (defeats the persistence economics) | Medium | Default tool returns text only; assert no `image_url` block in stored `Message` rows (AC-7) |
| Cloud vision re-process metered as $0 / unbounded | Medium | FRE-691 pre-flight estimate + threshold confirm, then the ADR-0065 gate reserve/commit (non-zero image-token) on the sub-agent call (AC-6) |
| Cost/vision events orphaned (not joinable to the turn) | Medium | Thread `trace_id`/`session_id`/`task_id`; ADR-0074 probe asserts zero orphans (AC-10) |
| Oversized stored image blows context/cost | Medium | Reuse per-image downscale + byte cap; over-limit content never reaches the model (AC-8) |
| `view_artifact_as_image` silently fails on a local build without `tool_result` image support | Low | Capability-gated + fail-closed with a message naming `analyze_artifact_image` (AC-11) |

---

## Implementation Notes

**Files affected:**

- `tools/artifact_tools.py` — new `analyze_artifact_image` executor + `ToolDefinition` (owner-scoped
  lookup, raster eligibility, `store.get` fetch, guardrail reuse, role-pinned vision `respond()`,
  text return); binary-branch honesty + redirect in `artifact_read_executor`; new
  `view_artifact_as_image` executor + definition (secondary, gated).
- `tools/__init__.py` — register the new tools.
- `config/governance/tools.yaml` — governance entries for both tools.
- `orchestrator/attachment_resolution.py` — reuse its guardrail/block-construction helpers; extract a
  shared `bytes → capped image_url block` helper if the current one is AttachmentRef-bound (small
  refactor, no behavior change).
- `orchestrator/attachment_cost.py` — reuse the pre-flight estimate + reservation for the sub-agent
  vision call.
- `orchestrator/sub_agent.py` / `llm_client` — role-pinned vision `respond()` (ADR-0077 seam); assert
  the pinned role's model has `supports_vision=true`, fail closed otherwise.
- `llm_client/models.py` + `config/models.yaml` — new `ModelDefinition.supports_tool_result_image` flag
  (§2, config-single-source per ADR-0099) for the secondary inline tool; `view_artifact_as_image`
  routes off it, **not** `supports_vision`. Defers with the inline tool if that path is deferred.
- Telemetry — thread `trace_id`/`session_id`/`task_id` onto fetch + sub-agent + cost events
  (ADR-0074).
- Tests — `tests/personal_agent/tools/` and `tests/personal_agent/orchestrator/`.

**Migration steps:** none (no schema change; new tools + a tool-behavior fix).

**Dependencies:** ADR-0069 (R2 `store.get`), ADR-0101/FRE-666 (resolution + guardrails), FRE-665
(capability routing), ADR-0065/FRE-691 (cost gate), ADR-0074 (joinability), ADR-0077 (sub-agent seam),
ADR-0044 (execution profiles).

**Testing strategy:** unit tests over `analyze_artifact_image` (owner-auth reject, raster eligibility
reject, `store.get` byte path with a mocked store, guardrail downscale/reject, vision `respond()`
mocked → text return, no image block in the persisted result); a routing test asserting the vision
sub-agent runs on a `supports_vision=true` model and fails closed on a local profile with no capable
local model; a cost test asserting a reservation precedes the vision call and a non-zero image-token
commit follows; an ADR-0074 joinability assertion (zero orphans); an `artifact_read` test asserting no
agent-fetchable URL remains and the redirect is present; and a master live smoke for the end-to-end
"agent sees a stored image and returns a marker-conditioned analysis" outcome.

---

## Verification / Acceptance Criteria

**How will we know this decision actually delivered — not just merged?**

- **AC-1 (stored image becomes vision, as text)** — `analyze_artifact_image(artifact_id)` on a stored
  raster image containing a **unique visual marker** (a word/object visible only in the image) returns
  an `analysis` text that references that marker. **Check:** a live tool call on a marker test image;
  assert the marker appears in the returned analysis. *Fails if* the analysis is generic or says it
  cannot see the image.
- **AC-2 (credentialed fetch, never CF Access)** — the analyzed bytes came from `store.get(r2_key)`,
  not an HTTP GET to the `public_url`. **Check:** an integration test asserts `store.get` is called
  with the artifact's `r2_key` and that no code path issues an HTTP GET to `artifacts_public_base_url`
  during the tool call. *Fails if* any path fetches the CF-Access URL for bytes.
- **AC-3 (authorization — own artifacts only)** — a session cannot analyze an artifact it does not own.
  **Check:** call the tool with another user's `artifact_id`; assert a not-found error **and** that
  `store.get` is **never** called (no bytes fetched). *Fails if* a non-owned artifact's bytes are
  fetched or analyzed.
- **AC-4 (eligibility — raster only)** — a non-raster artifact is rejected before any fetch or vision
  call. **Check:** call the tool on a `application/pdf` (and a `text/plain`) artifact; assert a clear
  rejection and that neither `store.get` nor a vision `respond()` runs. *Fails if* a non-image artifact
  reaches the vision model.
- **AC-5 (vision-capable routing, fail-closed local)** — the vision sub-agent runs on a model with
  `supports_vision=true`; on a local profile whose local model is non-vision, the tool fails closed
  with no cloud call. **Check:** (a) assert the role the sub-agent pins resolves to a
  `supports_vision=true` model; (b) with a local profile and a non-vision local model, assert
  `AttachmentUnsupportedError` is raised and **no** cloud `respond()` is issued. *(The (b) branch needs
  a test fixture/override pinning a **non-vision** local model, since the shipped local `primary` /
  `sub_agent` definitions are `supports_vision=true` — the fail-closed branch must be exercised via that
  override, not assumed.)* *Fails if* a non-vision model receives the image block, or a local-profile
  re-process silently reaches cloud.
- **AC-6 (cost gated before spend)** — a cloud vision re-process reserves against the ADR-0065 gate
  **before** the vision call and commits a **non-zero** cost whose token basis includes image tokens.
  **Check:** assert a cost-gate reservation is recorded before the sub-agent `respond()`; assert the
  commit records a non-zero `actual_cost` from `litellm.completion_cost()` with an image-token basis.
  *Fails if* the vision call precedes any reservation, or the cost is $0 / text-only.
- **AC-7 (no image block persists; the analysis text does)** — after the call, **no** raw image block
  is persisted for the turn, and the analysis text **is** present in what persists. **Check:** assert
  that **no** `image_url` / `image` content block appears in any stored `Message` row or persisted
  tool-result payload for the turn, and that the persisted tool-result payload **contains** the
  returned `analysis` string (substring containment — the payload may be a JSON serialization of
  `{analysis, disclosures}`, so require containment, **not** byte-equality). *Fails if* a raw image
  block is persisted (defeating the persistence economics) **or** the analysis text is absent from
  what persists.
- **AC-8 (guardrails fail closed)** — an over-cap stored image (pixel-dimension or byte size) is
  downscaled below the cap before the vision call or rejected; the over-limit bytes never reach the
  model. **Check:** parametrized over-cap inputs; assert the resolved block is below the cap or the
  call is rejected, and the over-limit content is absent from the vision `respond()` payload. *Fails
  if* any dimension passes through unbounded.
- **AC-9 (`artifact_read` honesty + redirect, at runtime)** — for a binary/image artifact, the
  **returned result of `artifact_read_executor`** exposes no field presenting a URL as an agent
  byte/content path, and the returned result **itself** carries a runtime redirect to
  `analyze_artifact_image`. **Check:** call `artifact_read_executor` on an image artifact and assert
  **on the returned dict** (not the static tool description) that (a) there is no bare `public_url` (or
  any field an agent would read as fetchable content) — any URL sits under a human-display-only key
  (e.g. `human_display_url`), and (b) a result field (e.g. `guidance`) names `analyze_artifact_image`
  as the way to see the image. *Fails if* the runtime binary-branch result still returns a bare
  `public_url`, **or** the redirect exists only in static tool metadata while the returned result path
  is unchanged.
- **AC-10 (joinability — zero orphans)** — the fetch, vision sub-agent, and cost events carry
  `trace_id` + `session_id` + `task_id` and join to the turn. **Check:** after a re-process turn, run
  the ADR-0074 joinability probe (`observability/`) and assert the cost row and vision/resolution
  events join to the turn's `(trace_id, session_id, task_id)` with **zero** orphans. *Fails if* any
  event lacks a join key or is orphaned per the probe.
- **AC-11 (secondary inline tool gated on a distinct signal + fail-closed)** — `view_artifact_as_image`
  delivers a raw image block only when the model's `supports_tool_result_image` flag is true, and
  otherwise fails closed naming `analyze_artifact_image`. **Check:** (a) with
  `supports_tool_result_image=true`, assert the tool result carries a typed image block; (b) with
  `supports_tool_result_image=false` **even when `supports_vision=true`**, assert a clear error naming
  `analyze_artifact_image` is returned, **no** image block is delivered, and no boundary crossing
  occurs. *Fails if* the tool routes off `supports_vision` instead of the tool-result-image signal,
  silently returns nothing usable, or crosses the profile boundary. *(If `view_artifact_as_image` is
  deferred, this AC and the `supports_tool_result_image` flag defer with its ticket; the assembled
  intent is then the AC-1..AC-10 subset — recorded explicitly, not silently dropped.)*

**Seam owner (decomposed ADR) — AC-SEAM.** The assembled intent holds only once every child lands; it
is owned by the **final live-smoke ticket**, run by master at the integration gate. The chain does
**not** close because the tool, cost, joinability, or `artifact_read`-fix ticket merged in isolation.

- **AC-SEAM (end-to-end, in one live run)** — In a single live session against the deployed stack: a
  stored, owned raster image with a visual marker → `analyze_artifact_image` → a **vision-capable**
  model → an `analysis` referencing the marker (AC-1 + AC-5); the result **persists as text with no
  stored image block** (AC-7); the cloud re-process **reserved and committed a non-zero image-token
  cost** (AC-6); the ADR-0074 probe reports **zero orphans** (AC-10); and `artifact_read` on the same
  artifact returns **no agent-fetchable URL** and redirects to the vision tool (AC-9). **Check:** master
  runs all legs live. *Fails if* any leg regresses — which it will unless the tool, credentialed fetch,
  eligibility/auth, guardrails, capability routing, cost gate, joinability threading, and the
  `artifact_read` fix have **all** landed.

---

## References

- ADR-0101 — Agent Vision Ingestion of Uploaded Images (Accepted) — current-turn foundation this ADR
  reuses; §5 Option 1 named the stored-image path as deferred v2
- ADR-0102 — Document Ingestion (Accepted) — PDF/OCR/chunking; owns the non-raster document path
  excluded here
- ADR-0069 — R2 artifact substrate (the `store.get` credentialed byte path)
- ADR-0070 — output channels (human-facing `public_url` display only)
- ADR-0065 — cost gate (pre-flight reservation + non-zero image-token commit)
- ADR-0074 — joinability (trace/session/task threading; the zero-orphan probe)
- ADR-0077 — plan/generate sub-agent seam (`get_llm_client(role_name=…)` → `respond`)
- ADR-0044 — execution profiles (cloud/local binding; the data-egress boundary)
- ADR-0099 — configuration management & validation (capability flags single-source)
- FRE-736 — this ADR's tracking issue
- FRE-666 — current-turn resolution + guardrails (`orchestrator/attachment_resolution.py`) reused here
- FRE-665 — capability-driven routing (`ModelDefinition.supports_vision`) reused here
- FRE-691 — cloud image cost gate (`orchestrator/attachment_cost.py`) reused here
- FRE-727 — no-cage principle (explicit tool over rigid auto-route)
- Code anchors — `tools/artifact_tools.py:594-699` (`artifact_read`), `:1410-1443` (sub-agent seam);
  `storage/artifact_store.py` (`store.get`); `orchestrator/attachment_resolution.py`;
  `orchestrator/attachment_cost.py`; `orchestrator/sub_agent.py:213` (`run_sub_agent`)

---

## Status Updates

### 2026-07-02 - Proposed
**Changed By:** lextra (adr session, Opus)
**Reason:** Design pass for FRE-736. Owner settled the three forks: explicit tool the model reaches for
(no-cage, FRE-727), **analyze-to-text as the default** mechanism (persistence economics — the analysis
persists cheaply; the main model need not be vision-capable) with a secondary gated inline-block tool,
and a **new ADR** (not an ADR-0101 amendment). Reuses ADR-0101's shipped foundation; folds the
`artifact_read` binary-URL defect (problem 2) in as the honesty + redirect fix.

---

**Template Version:** 1.1
**Based On:** [Michael Nygard's ADR pattern](https://cognitect.com/blog/2011/11/15/documenting-architecture-decisions)
