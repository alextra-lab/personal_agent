# FRE-666: Resolve raster image attachments to content blocks at turn assembly

**Ticket 4 of the ADR-0101 chain.** Backing: ADR-0101 Decision §3, §4, §6; ADR-0069.
Branch: `fre-666-vision-raster-resolve`.

## Scope

This ticket builds the **resolution mechanism only** — not routing/capability assertion
(FRE-665, already Done, PR #306), not cost-gate reservation (FRE-691), not joinability
(FRE-693), not the `artifact_read` `public_url` honesty fix (AC-8, separate ticket). Concretely:

1. A new attachment-resolution module: credentialed byte fetch, content-type validation,
   guardrails (per-image pixel cap w/ downscale, per-image byte cap, images-per-turn cap,
   total-payload cap), image block construction.
2. The turn-assembly injection site in `orchestrator/executor.py` (`step_init`, the
   `ctx.messages.append({"role": "user", ...})` call) — inject the resolved block list into
   the initial user message.
3. Configuration for the four caps.

**Acceptance criteria owned:** AC-2 (credentialed fetch, never CF Access), AC-3 (typed block in
the initial message), AC-7 (every guardrail dimension fails closed, downscale/drop cases disclose
per the FRE-690 amendment).

## Design decisions

Revised after a codex plan-review pass (2026-07-02) surfaced two real bugs and one real scope
gap in the first draft — see "Changes from codex review" below for what moved and why.

- **Non-raster attachments known to the upload allowlist are silently left unresolved; a truly
  unrecognized content type is rejected.** `resolve_attachments` checks each attachment against
  two closed sets: `RASTER_CONTENT_TYPES` (processed) and `_KNOWN_NON_RASTER_CONTENT_TYPES` — the
  exact non-raster subset of `uploads_router.ALLOWED_UPLOAD_CONTENT_TYPES`
  (`application/pdf`, `text/plain`, `text/markdown`, `text/csv`, `application/json`,
  `image/svg+xml`) — which is skipped silently (out of this ADR's v1 scope; ADR-0102 and SVG are
  explicitly out of scope per the ADR's own Scope section, so rejecting them would break
  legitimate non-image uploads coexisting in the same turn). Anything in **neither** set is
  genuinely mismatched/unsupported and raises `AttachmentUnsupportedError` — this is the concrete
  reading of "a mismatched or unsupported declared content type is rejected with a clear
  user-visible error, never silently downgraded" that doesn't also break PDF/text attachments the
  ADR explicitly scopes elsewhere. A test asserts
  `RASTER_CONTENT_TYPES | _KNOWN_NON_RASTER_CONTENT_TYPES == ALLOWED_UPLOAD_CONTENT_TYPES` so the
  two lists (this module's and `uploads_router.py`'s) can't silently drift apart.
- **Guardrail failure mode per dimension** — caps are enforced against the **base64-encoded**
  payload size (the `data:` URI content that actually reaches the model), not raw image bytes;
  see "Changes from codex review" item 2:
  - per-image **pixel** cap over limit → **downscale** (Pillow, aspect-preserving) → **disclose**.
  - per-image **encoded-byte** cap still over limit *after* downscale → **reject the turn**
    (`AttachmentUnsupportedError`, existing exception from FRE-665) — no disclosure needed, the
    turn fails outright with a message naming the image.
  - **images-per-turn** cap exceeded → **drop** the excess (keep the first N in submitted order)
    → **disclose** the drop count.
  - **total-encoded-payload** cap exceeded → **drop** trailing images once the cumulative encoded
    size would exceed the cap → **disclose** the drop count.
  This gives every dimension an unambiguous, independently-testable behavior and satisfies the
  AC-7 test structure (one dimension rejects, three transform-and-disclose).
- **Reuse `AttachmentUnsupportedError`** (`personal_agent/exceptions.py`, introduced by FRE-665)
  for both guardrail rejection and the content-type-mismatch case, rather than adding a new
  exception type. It is already wired into `error_classification.classify_error` with a dedicated
  `attachment_unsupported` category, and raising it from `step_init` bubbles to `ctx.error` via
  the existing top-level `except Exception` in `execute_task` — the same mechanism FRE-665 already
  uses for the routing-fail-closed case. No new error-handling plumbing needed. Its docstring is
  broadened by one sentence (Step 5 below) since it now covers resolution/guardrail failures too,
  not only routing failures — codex flagged the original routing-only docstring as narrower than
  its actual usage.
- **Disclosure delivery: deterministically appended to `ctx.final_reply` in `step_synthesis`**,
  not a separate `OrchestratorResult` key. Codex correctly caught that the original design (a new
  `OrchestratorResult["attachment_disclosures"]` key) never reaches the user — `service/app.py`
  only reads `result.get("reply")` for the assistant's response; a sibling dict key is inert. A
  `ctx.attachment_disclosures: list[str]` field is still added to `ExecutionContext` (populated by
  `resolve_attachments` at turn assembly), but the disclosure text is joined and appended to
  `ctx.final_reply` in `step_synthesis` (`executor.py`, right after the `"Task completed"`
  fallback) — guaranteed to land in the actual reply text and the persisted session message, with
  no dependency on the model choosing to relay it.
- **Relocate `_RASTER_IMAGE_CONTENT_TYPES`** (currently private, `executor.py:1329`, built for
  FRE-665's routing seam) into the new module as `RASTER_CONTENT_TYPES`, and have the routing seam
  import it. Two independently-maintained copies of the same allowlist is exactly the drift risk
  ADR-0101's risk table calls out ("declared content_type is wrong"); there are only two
  call sites today (both in `executor.py`), so this is a 3-line change, not a refactor. Codex
  confirmed no circular-import risk (the new module's own imports don't reach back into
  `executor.py`).
- **`get_artifact_store()` returning `None`** (R2 unwired) with a raster attachment present →
  fail closed with `AttachmentUnsupportedError`, matching the existing `if store is None:` guard
  pattern in `uploads_router.py` / `artifact_tools.py`.
- **Cap defaults** (guardrail sizing, not a cost/budget threshold — no owner pre-approval needed
  per the budget-change policy, but flagged here for visibility): per-image long-edge pixel cap
  **1568px** (Anthropic's own vision downscale threshold — resizing to it ourselves keeps local
  and cloud paths visually consistent); per-image **encoded** byte cap **5 MiB**; max **4** images
  per turn; total per-turn **encoded** payload cap **15 MiB**.
- **Animated GIF downscale is a known v1 simplification, not a bug to fix here.** Pillow's
  `thumbnail()` on a multi-frame GIF operates on the loaded (first) frame only; a downscaled
  animated GIF attachment collapses to a still. Not covered by any AC on this ticket — noted as a
  code comment at the call site, not solved (no test requires animation preservation).

### Changes from codex review

A codex plan-review pass on the first draft of this plan found two correctness bugs and one real
scope gap, all fixed in the design above:

1. **Disclosure never reached the user.** The original plan added a bare `OrchestratorResult`
   dict key that nothing downstream reads — `service/app.py` only surfaces `result["reply"]` as
   the assistant's response. Fixed by appending disclosures to `ctx.final_reply` in
   `step_synthesis` instead (deterministic, no new response-plumbing needed).
2. **Guardrail caps compared against raw bytes, not the encoded payload that reaches the
   model.** ADR-0101 §6 says the byte cap applies "after downscale/**encode**"; base64 inflates
   size ~33%. Fixed by computing `base64.b64encode(...)` before both the per-image and
   total-payload cap checks (order changed accordingly in Step 2 below).
3. **Non-raster attachments were unconditionally silent, with no boundary against a genuinely
   unrecognized content type.** The ADR's "rejected with a clear user-visible error" language
   needs *some* enforcement point, but blanket-rejecting all non-raster types would break
   legitimate PDF/text attachments this ADR explicitly scopes elsewhere. Fixed by checking against
   the *known* non-raster set (mirrors `uploads_router.ALLOWED_UPLOAD_CONTENT_TYPES`) and only
   rejecting what's in neither set, with a drift-guard test tying the two lists together.

## Step 1 — guardrail config

**File:** `src/personal_agent/config/settings.py` (near the existing `upload_max_size_bytes` /
`artifact_draft_max_tokens` fields, ~line 695)

```python
attachment_image_max_pixels: int = Field(
    default=1568,
    gt=0,
    description=(
        "Per-image long-edge pixel cap before encoding (ADR-0101 §6). An "
        "over-limit image is downscaled (aspect-preserving) below this before "
        "the byte-size check. Matches Anthropic's own vision downscale threshold."
    ),
)
attachment_image_max_bytes: int = Field(
    default=5_242_880,  # 5 MiB
    gt=0,
    description=(
        "Per-image byte-size cap after downscale/encode (ADR-0101 §6). An "
        "image still over this cap after downscale is rejected with a "
        "user-visible AttachmentUnsupportedError."
    ),
)
attachment_max_images_per_turn: int = Field(
    default=4,
    gt=0,
    description=(
        "Max raster images resolved per turn (ADR-0101 §6). Excess images "
        "(beyond the first N in submitted order) are dropped with disclosure."
    ),
)
attachment_max_total_payload_bytes: int = Field(
    default=15_728_640,  # 15 MiB
    gt=0,
    description=(
        "Total per-turn resolved-image payload cap across all images "
        "(ADR-0101 §6), independent of the per-image byte cap. Trailing "
        "images that would exceed it are dropped with disclosure."
    ),
)
```

**Test:** `tests/test_config/test_settings.py` (or wherever existing cap fields like
`upload_max_size_bytes` are asserted) — add default-value assertions for the four new fields.

## Step 2 — `attachment_resolution.py` module

**File:** `src/personal_agent/orchestrator/attachment_resolution.py` (new)

```python
"""Resolve current-turn raster attachments to image content blocks.

ADR-0101 §3 (credentialed fetch), §4 (block construction), §6 (guardrails). Ticket 4
of the ADR-0101 chain (FRE-666) — routing (FRE-665) and cost gating (FRE-691) are
separate concerns; this module only turns bytes into a typed, capped image block list.
"""

from __future__ import annotations

import base64
from collections.abc import Sequence
from dataclasses import dataclass
from io import BytesIO
from typing import Any

from PIL import Image

from personal_agent.config import settings
from personal_agent.exceptions import AttachmentUnsupportedError
from personal_agent.orchestrator.types import AttachmentRef
from personal_agent.storage import get_artifact_store
from personal_agent.telemetry import get_logger

log = get_logger(__name__)

RASTER_CONTENT_TYPES: frozenset[str] = frozenset(
    {"image/png", "image/jpeg", "image/gif", "image/webp"}
)

# The non-raster subset of uploads_router.ALLOWED_UPLOAD_CONTENT_TYPES — attachments of these
# types coexist validly in a turn (ADR-0102 documents, SVG) and are left unresolved by this
# module, not rejected. A test ties this to the upload allowlist so the two can't silently drift.
_KNOWN_NON_RASTER_CONTENT_TYPES: frozenset[str] = frozenset(
    {
        "application/pdf",
        "text/plain",
        "text/markdown",
        "text/csv",
        "application/json",
        "image/svg+xml",
    }
)

_PIL_FORMAT_BY_CONTENT_TYPE: dict[str, str] = {
    "image/png": "PNG",
    "image/jpeg": "JPEG",
    "image/gif": "GIF",
    "image/webp": "WEBP",
}


@dataclass(frozen=True)
class ResolvedAttachments:
    """Result of resolving a turn's raster attachments to content blocks.

    Attributes:
        blocks: OpenAI-style ``image_url`` content blocks, one per resolved image,
            in submitted order.
        disclosures: User-facing strings describing any downscale/drop applied by
            a guardrail (ADR-0101 §6, FRE-690 disclose-on-alter).
    """

    blocks: tuple[dict[str, Any], ...]
    disclosures: tuple[str, ...]


def _downscale_if_needed(image_bytes: bytes, content_type: str) -> tuple[bytes, bool]:
    """Downscale ``image_bytes`` below the pixel cap if either dimension exceeds it.

    Args:
        image_bytes: Original fetched image bytes.
        content_type: Declared raster MIME type (selects the re-encode format).

    Returns:
        ``(bytes, was_downscaled)`` — original bytes unchanged and ``False`` when
        already within the cap, else re-encoded bytes and ``True``.

    Note:
        Pillow's ``thumbnail()`` operates on the currently-loaded frame only; a
        downscaled animated GIF collapses to a still. Accepted v1 simplification —
        no AC on this ticket requires animation preservation.
    """
    with Image.open(BytesIO(image_bytes)) as img:
        if max(img.size) <= settings.attachment_image_max_pixels:
            return image_bytes, False
        img = img.copy()
        img.thumbnail(
            (settings.attachment_image_max_pixels, settings.attachment_image_max_pixels),
            Image.Resampling.LANCZOS,
        )
        buf = BytesIO()
        img.save(buf, format=_PIL_FORMAT_BY_CONTENT_TYPE[content_type])
        return buf.getvalue(), True


async def resolve_attachments(
    attachments: Sequence[AttachmentRef], *, trace_id: str | None = None
) -> ResolvedAttachments:
    """Resolve a turn's raster attachments into capped, typed image content blocks.

    Fetches bytes via the credentialed R2 ``store.get`` path (never a public URL),
    enforces the four ADR-0101 §6 guardrails independently against the base64-encoded
    payload size (what actually reaches the model), and builds one OpenAI-style
    ``image_url`` block per surviving image.

    Args:
        attachments: The turn's structured attachment carrier (FRE-661). Known
            non-raster entries (documents, SVG) are left unresolved (out of this
            ADR's v1 scope); a content type in neither set is rejected.
        trace_id: Originating request trace_id, threaded onto the ``store.get`` call
            and failure logs (ADR-0074 identity threading).

    Returns:
        ``ResolvedAttachments`` with the surviving image blocks and any disclosure
        strings for downscaled/dropped images.

    Raises:
        AttachmentUnsupportedError: A declared content type is neither a supported
            raster type nor a known non-raster type; an image is still over the
            per-image encoded-byte cap after downscale; or R2 storage is not
            configured.
    """
    raster: list[AttachmentRef] = []
    for attachment in attachments:
        if attachment.content_type in RASTER_CONTENT_TYPES:
            raster.append(attachment)
        elif attachment.content_type in _KNOWN_NON_RASTER_CONTENT_TYPES:
            continue
        else:
            raise AttachmentUnsupportedError(
                f"Attachment '{attachment.title}' has an unsupported content type: "
                f"{attachment.content_type}."
            )

    if not raster:
        return ResolvedAttachments(blocks=(), disclosures=())

    disclosures: list[str] = []

    if len(raster) > settings.attachment_max_images_per_turn:
        cap = settings.attachment_max_images_per_turn
        dropped = len(raster) - cap
        raster = raster[:cap]
        disclosures.append(
            f"Only the first {cap} images were included; {dropped} image(s) were "
            "dropped (per-turn image limit)."
        )

    store = get_artifact_store()
    if store is None:
        raise AttachmentUnsupportedError(
            "Image attachments cannot be processed: artifact storage is not configured."
        )

    blocks: list[dict[str, Any]] = []
    total_encoded_bytes = 0
    payload_dropped = 0

    for attachment in raster:
        raw = await store.get(attachment.r2_key, trace_id=trace_id)
        image_bytes, was_downscaled = _downscale_if_needed(raw, attachment.content_type)
        encoded = base64.b64encode(image_bytes).decode("ascii")
        encoded_len = len(encoded)

        if encoded_len > settings.attachment_image_max_bytes:
            raise AttachmentUnsupportedError(
                f"Image '{attachment.title}' is too large ({encoded_len} encoded bytes) "
                f"even after downscaling; the per-image limit is "
                f"{settings.attachment_image_max_bytes} bytes."
            )

        if total_encoded_bytes + encoded_len > settings.attachment_max_total_payload_bytes:
            payload_dropped += 1
            continue

        total_encoded_bytes += encoded_len
        if was_downscaled:
            disclosures.append(f"Image '{attachment.title}' was downscaled to fit the size limit.")

        blocks.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:{attachment.content_type};base64,{encoded}"},
            }
        )

    if payload_dropped:
        disclosures.append(
            f"{payload_dropped} image(s) were dropped because the total per-turn "
            "attachment payload limit was reached."
        )

    return ResolvedAttachments(blocks=tuple(blocks), disclosures=tuple(disclosures))
```

**Tests:** `tests/personal_agent/orchestrator/test_attachment_resolution.py` (new, mirrors
`test_attachment_carrier.py`'s location/conventions)

- `test_known_non_raster_attachments_produce_no_blocks` — a `content_type="application/pdf"`
  attachment → `blocks == ()`, `disclosures == ()`, `store.get` never called, no error raised.
- `test_unrecognized_content_type_rejected` — a `content_type="image/tiff"` (in neither
  `RASTER_CONTENT_TYPES` nor `_KNOWN_NON_RASTER_CONTENT_TYPES`) attachment →
  `pytest.raises(AttachmentUnsupportedError)`.
- `test_raster_and_non_raster_sets_cover_the_upload_allowlist` (drift guard) — asserts
  `RASTER_CONTENT_TYPES | _KNOWN_NON_RASTER_CONTENT_TYPES ==
  uploads_router.ALLOWED_UPLOAD_CONTENT_TYPES`.
- `test_store_get_called_with_r2_key_and_trace_id` (**AC-2**) — mock `get_artifact_store` to
  return a mock store (`AsyncMock` on `.get`); assert `store.get` awaited with
  `(attachment.r2_key,)` / `trace_id=...`; separately patch `httpx.AsyncClient` (or the module's
  only outbound-HTTP surface) and assert it is never touched during resolution.
- `test_resolved_block_is_openai_image_url_shape` (**AC-3** at the unit level) — one small PNG
  fixture (generate via `PIL.Image.new` in-test) within all caps → one block,
  `block["type"] == "image_url"`, `block["image_url"]["url"].startswith("data:image/png;base64,")`.
- `test_pixel_cap_downscales_and_discloses` (**AC-7** dimension 1) — fixture image larger than a
  monkeypatched low `attachment_image_max_pixels`; assert the decoded output image's
  `max(size) <= cap`; assert a disclosure mentioning the image title is present.
- `test_byte_cap_rejects_after_downscale` (**AC-7** dimension 2) — monkeypatch
  `attachment_image_max_bytes` to an unreachably low value; assert
  `pytest.raises(AttachmentUnsupportedError)`; assert no block was appended before the raise
  (nothing partial leaks — construct the call so this is observable, e.g. single-image input).
  Assert the cap is compared against the **encoded** length (e.g. craft raw bytes whose length is
  under the cap but whose base64 encoding is over it, and confirm the raise still fires).
- `test_images_per_turn_cap_drops_excess` (**AC-7** dimension 3) — 5 raster attachments,
  monkeypatch `attachment_max_images_per_turn=2`; assert `len(blocks) == 2`, blocks correspond to
  the first two in submitted order, disclosure mentions the dropped count.
- `test_total_payload_cap_drops_trailing` (**AC-7** dimension 4) — 3 images whose **encoded**
  sizes are crafted so the 3rd would push cumulative encoded bytes over a monkeypatched
  `attachment_max_total_payload_bytes`; assert `len(blocks) == 2`, disclosure mentions 1 dropped.
- `test_store_unconfigured_raises` — `get_artifact_store` patched to return `None`; assert
  `AttachmentUnsupportedError`.

## Step 3 — relocate the raster-type constant, turn-assembly injection

**File:** `src/personal_agent/orchestrator/executor.py`

1. Delete `_RASTER_IMAGE_CONTENT_TYPES` (line 1329) and the local definition; replace the
   `_resolve_vision_routing_key` reference (line 1355) with
   `from personal_agent.orchestrator.attachment_resolution import RASTER_CONTENT_TYPES` (import
   already inside that function, alongside its other local imports) and use
   `RASTER_CONTENT_TYPES` in place of `_RASTER_IMAGE_CONTENT_TYPES`.
2. At the turn-assembly site (`step_init`, currently line 1836):

```python
    # Add new user message — resolve current-turn raster attachments to image
    # blocks first (ADR-0101 §3/§4/§6, FRE-666); widens content to a block list
    # only when there is something to inject (FRE-664 MessageContent).
    content: MessageContent = ctx.user_message
    if ctx.attachments:
        from personal_agent.orchestrator.attachment_resolution import resolve_attachments

        resolved = await resolve_attachments(ctx.attachments, trace_id=ctx.trace_id)
        ctx.attachment_disclosures = list(resolved.disclosures)
        if resolved.blocks:
            content = (
                [{"type": "text", "text": ctx.user_message}, *resolved.blocks]
                if ctx.user_message
                else list(resolved.blocks)
            )
    ctx.messages.append({"role": "user", "content": content})
```

Add `from personal_agent.llm_client.message_content import MessageContent` to the module's
existing import block (top-level, matching how other FRE-664 sites import it — check
`_validate_and_fix_conversation_roles` for the exact existing import path/style).

**Note:** `AttachmentUnsupportedError` raised inside `resolve_attachments` propagates unmodified
out of `step_init` (no local try/except) to the existing top-level `except Exception as e: ...
ctx.error = e` in `execute_task` — the same fail-closed path FRE-665 already relies on for routing
rejections. No new error-handling code needed here.

**Tests:** `tests/test_orchestrator/test_executor.py` — new test class near the existing
`step_init` tests (~line 1090), following the `_make_ctx` / direct `step_init(...)` call pattern:

- `test_no_attachments_content_stays_string` — `ctx.attachments == ()` →
  `ctx.messages[-1]["content"] == ctx.user_message` (str, unchanged from today).
- `test_image_attachment_injects_block_list` (**AC-3**) — one raster `AttachmentRef`, mock
  `resolve_attachments` (patch at `personal_agent.orchestrator.executor.resolve_attachments` —
  it's imported locally inside the function, so patch the source module
  `personal_agent.orchestrator.attachment_resolution.resolve_attachments` instead, or restructure
  the import to module-level if that patch target proves awkward) to return one image block;
  assert `ctx.messages[-1]["content"]` is a `list` containing a `type == "image_url"` block and a
  `type == "text"` block with `ctx.user_message`.
- `test_empty_user_message_with_attachment_omits_text_block` — `user_message=""` + one resolved
  image block → content list has only the image block, no empty text block.
- `test_disclosures_copied_onto_ctx` — `resolve_attachments` returns non-empty `disclosures` →
  `ctx.attachment_disclosures` matches.

## Step 4 — carry disclosures onto `ExecutionContext` and append them to the actual reply

**File:** `src/personal_agent/orchestrator/types.py`

Add near `attachments` (line 297):

```python
    # --- FRE-666 / ADR-0101 §6 guardrail disclosure ---
    # User-facing strings describing any downscale/drop a guardrail applied while
    # resolving this turn's raster attachments. Appended to ctx.final_reply by
    # step_synthesis so the disclosure reaches the user (never silent) — see
    # "Changes from codex review" in Design decisions for why this isn't a
    # separate OrchestratorResult key (nothing downstream reads one).
    attachment_disclosures: list[str] = field(default_factory=list)
```

`OrchestratorResult` is unchanged — no new key. Disclosures ride the existing `reply` field.

**File:** `src/personal_agent/orchestrator/executor.py` — `step_synthesis` (~line 3695-3698), right
after the fallback check:

```python
        # Ensure final reply is set (should already be set from LLM call)
        if not ctx.final_reply:
            ctx.final_reply = "Task completed"  # Fallback

        # ADR-0101 §6 / FRE-690: guardrail alterations (downscale/drop) are disclosed
        # in the response, deterministically — never left to the model to relay.
        if ctx.attachment_disclosures:
            disclosure_text = "\n\n".join(f"Note: {d}" for d in ctx.attachment_disclosures)
            ctx.final_reply = f"{ctx.final_reply}\n\n{disclosure_text}"
```

This guarantees the disclosure lands in `ctx.final_reply` — which is both the persisted session
message (`session_manager.update_session(ctx.session_id, messages=ctx.messages)`, a few lines
below in the same function) and `result["reply"]` in `execute_task_safe` — with no dependency on
the model choosing to mention it, and no new response-shape plumbing through `service/app.py`.

**Test:** `tests/test_orchestrator/test_executor.py` — extend the `step_synthesis` tests (or add a
small new class):

- `test_disclosures_appended_to_final_reply` — `ctx.final_reply = "Here's what I see."`,
  `ctx.attachment_disclosures = ["Image 'a.png' was downscaled to fit the size limit."]` →
  after `step_synthesis`, `ctx.final_reply` contains both the original text and the disclosure.
- `test_no_disclosures_leaves_final_reply_unchanged` — empty `attachment_disclosures` → 
  `ctx.final_reply` unchanged from its pre-`step_synthesis` value.

## Step 5 — broaden `AttachmentUnsupportedError`'s docstring

**File:** `src/personal_agent/exceptions.py`

Codex noted the existing docstring is scoped to routing/capability failures only (FRE-665); this
ticket reuses the same exception for resolution/guardrail failures too. One-sentence addition,
no behavior change:

```python
class AttachmentUnsupportedError(ValueError):
    """Raised when a turn's attachment cannot be delivered to the model.

    Covers two related fail-closed cases (ADR-0101): routing (§5/§8a) — no
    reachable model can serve the attachment — and resolution (§6, FRE-666) — a
    guardrail cap is exceeded after transformation, or the declared content type
    is neither a supported raster type nor a known non-raster type. Always raised
    with a message naming the unsupported modality, surfaced to the user verbatim.
    """
```

## Acceptance criteria → test map

| AC | Test(s) |
|----|---------|
| AC-2 (credentialed fetch, never CF Access) | `test_store_get_called_with_r2_key_and_trace_id` |
| AC-3 (typed block in initial message) | `test_resolved_block_is_openai_image_url_shape`, `test_image_attachment_injects_block_list` |
| AC-7 (every guardrail dimension fails closed) | `test_pixel_cap_downscales_and_discloses`, `test_byte_cap_rejects_after_downscale`, `test_images_per_turn_cap_drops_excess`, `test_total_payload_cap_drops_trailing`, `test_disclosures_appended_to_final_reply` |

## Test commands

```bash
make test-file FILE=tests/personal_agent/orchestrator/test_attachment_resolution.py
make test-file FILE=tests/test_orchestrator/test_executor.py
make test-file FILE=tests/personal_agent/orchestrator/test_attachment_carrier.py
make test-file FILE=tests/test_config/test_settings.py
make mypy
make ruff-check
make ruff-format
```

## Out of scope (do not build here)

- Routing/capability assertion — FRE-665, already Done.
- Cost estimate/reservation for cloud image calls — FRE-691.
- `trace_id`/`session_id`/`task_id` joinability on resolution events — FRE-693.
- `artifact_read` `public_url` honesty fix (AC-8) — separate ticket, not in this chain's blockers.
- PWA per-attachment override affordance — FRE-692.
