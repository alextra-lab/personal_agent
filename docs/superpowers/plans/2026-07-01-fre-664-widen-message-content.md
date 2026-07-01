# FRE-664: widen message content to support block lists + harden string-assuming sites

**Backing:** ADR-0101 §2, Negative Consequences ("Content type widening — a non-trivial blast
radius"), Implementation Notes. Ticket 2 of the ADR-0101 chain (blocked by FRE-661, merged
`1bca254`). Ticket 4 (image resolution/injection) depends on this landing first.

**Acceptance criteria slice:** AC-3 (typed block survives the pipeline) — a message whose content
is a list containing a text block and an image block must pass through every audited site with the
block list preserved (not collapsed to a string or emptied), and the assembled `request_messages`
must carry that list content on the user turn intact.

## Scope (from the ticket body)

1. The persisted message model type (`service/models.py:130`).
2. A shared block-aware text accessor.
3. The orchestrator executor sites named in ADR-0101's Negative Consequences / Implementation Notes.
4. The context-window token estimator (`context_window.py:30-32`).

Out of scope (per the ticket's own Scope paragraph, not "audit the whole repo"): compression_manager,
context_compressor, tool_result_digest, sub_agent, gateway/session_api, gateway/chat_api,
request_gateway/* — none of these are named in ADR-0101, and no real image block exists yet (that's
ticket 4). `litellm_client.py` is already block-aware (isinstance-checked throughout — it's the
"wire layer already tolerates list content" the ADR cites as grounding) and needs no change.

## Step 1 — Shared block-aware accessor (new module)

**File:** `src/personal_agent/llm_client/message_content.py` (new)

Lives in `llm_client` because it has zero dependency on `service`/`orchestrator` today (verified via
grep) and both of those layers already depend on `llm_client` — the correct direction for a shared
low-level type/helper. `history_sanitiser.py`'s `isinstance(content, str)` guard is the template
cited by the ADR; this module generalizes it into reusable helpers instead of repeating the guard
ad hoc at every site.

```python
"""Block-aware helpers for message ``content`` fields (ADR-0101 SS2, FRE-664).

``content`` widens from a plain ``str`` to ``str | list[dict[str, Any]]`` once an
attachment resolves to a typed content block (e.g. ``image_url``, ticket 4). Every
site that previously assumed ``str`` must route through these helpers so list
content degrades safely — text preserved, non-text blocks skipped — instead of
being silently stringified, corrupted, or collapsed to an empty string.
"""

from __future__ import annotations

from typing import Any

from personal_agent.llm_client.token_counter import estimate_tokens

MessageContent = str | list[dict[str, Any]]

# Fixed per-image token estimate (ADR-0101 SS8b: "~1600 tokens max after resize").
# Used for context-window budgeting until an image is actually resolved; ticket 4's
# resolution module may pass provider-reported usage in its own follow-up.
IMAGE_BLOCK_TOKEN_ESTIMATE = 1600


def get_text_content(content: Any) -> str:
    """Extract the text portion of a message ``content`` field.

    Args:
        content: Either a plain string or a list of typed content blocks
            (e.g. ``{"type": "text", "text": ...}``, ``{"type": "image_url", ...}``).

    Returns:
        The string unchanged; the blank-line-joined text of every ``text``-type
        block for list content; or ``""`` for anything else (``None``, an
        image-only block list, or an unrecognized/malformed shape).
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "text":
                continue
            text = block.get("text", "")
            if isinstance(text, str) and text:
                parts.append(text)
        return "\n\n".join(parts)
    return ""


def merge_content(old: Any, new: Any) -> Any:
    """Merge two message ``content`` fields (duplicate-role-merge helper).

    String + string keeps the historical ``"{old}\\n\\n{new}"`` behavior. Once
    either side is a list of blocks, string-interpolating would corrupt or drop
    the block(s) (an f-string over a list embeds its Python repr) — instead both
    sides are normalized to block lists and concatenated, so every block from
    both sides survives in order.

    Args:
        old: Prior message's content.
        new: Incoming message's content being merged in.

    Returns:
        Merged content: a string when both inputs were strings (or empty), else
        a list of content blocks.
    """
    if not isinstance(old, list) and not isinstance(new, list):
        old_s, new_s = (old or ""), (new or "")
        if old_s and new_s:
            return f"{old_s}\n\n{new_s}"
        return new_s or old_s

    def _as_blocks(c: Any) -> list[dict[str, Any]]:
        if isinstance(c, list):
            return [b for b in c if isinstance(b, dict)]
        if isinstance(c, str) and c:
            return [{"type": "text", "text": c}]
        return []

    return _as_blocks(old) + _as_blocks(new)


def count_content_tokens(content: Any) -> int:
    """Estimate token count for a message ``content`` field, block-aware.

    Args:
        content: Plain string or list of typed content blocks.

    Returns:
        ``estimate_tokens`` over the full string for ``str`` content; for list
        content, the sum of ``estimate_tokens`` over each text block's text plus
        ``IMAGE_BLOCK_TOKEN_ESTIMATE`` for each non-text block. ``0`` for
        anything else.
    """
    if isinstance(content, str):
        return estimate_tokens(content)
    if isinstance(content, list):
        total = 0
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                text = block.get("text", "")
                total += estimate_tokens(text) if isinstance(text, str) else 0
            else:
                total += IMAGE_BLOCK_TOKEN_ESTIMATE
        return total
    return 0
```

**Test:** `tests/personal_agent/llm_client/test_message_content.py` (new) — unit tests for all three
functions: str passthrough, text-block extraction, image-only list -> `""`, merge str+str (unchanged
behavior), merge list+str and str+list (blocks preserved, order preserved), merge list+list, token
count for str vs. block list (text tokens + fixed image estimate), `None`/empty/malformed content.

## Step 2 — Widen the persisted `Message` model

**File:** `src/personal_agent/service/models.py`

```python
from personal_agent.llm_client.message_content import MessageContent
...
class Message(BaseModel):
    """A single message in conversation."""

    role: str  # 'user', 'assistant', 'system', 'tool'
    content: MessageContent
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    metadata: dict[str, Any] = Field(default_factory=dict)
```

No behavior to test here beyond a type-level smoke (pydantic accepts both a `str` and a
`list[dict]` for `content`) — add one assertion to the new test file in Step 5.

## Step 3 — Harden `orchestrator/executor.py` sites

All edits import `get_text_content` / `merge_content` from
`personal_agent.llm_client.message_content` at the top of the module (alongside the existing
`llm_client` imports already there).

**3a. Duplicate-role merge (`_validate_and_fix_conversation_roles`, ~L716-721)** — replace the
string-interpolation merge with `merge_content`:

```python
old_content = prior.get("content", "")
new_content = msg.get("content", "")
prior["content"] = merge_content(old_content, new_content)
```

(The `log.warning` a few lines below previews `new_content` via `str(new_content)[:50]` — leave the
log line's `str()` call as-is; it is a debug preview of the *incoming* message being merged, not the
final content field, and truncated reprs there are an accepted existing pattern elsewhere. Not one
of the ADR's named sites.)

**3b. No-think tool-prompt injection (`_append_no_think_to_last_user_message`, ~L803-816)** — fix a
real bug the ADR's line reference flags: when the last `user` message has non-`str` content, the
current `continue` falls through to an **earlier** user message and injects the suffix there
instead — silently misapplying `/no_think` to the wrong turn. Fix: stop the search instead.

```python
for i in range(len(out) - 1, -1, -1):
    if out[i].get("role") != "user":
        continue
    content = out[i].get("content")
    if not isinstance(content, str):
        # Block-list content (e.g. an image attachment) — do not stringify it,
        # and do not fall through to an older user message either.
        return out
    trimmed = content.rstrip()
    ...
```

**3c. `_append_no_think_synthesis_nudge` (~L836-841)** — already type-guards correctly (returns
`out` unmutated when `out[-1]` content isn't `str`, no fallthrough bug). No change; covered by a
regression test in Step 5 to prove it (audit-proof, not just audit-assert).

**3d. `_inline_volatile_into_last_user_message` (~L882-887)** — already returns the original
`messages` unmutated when the last user content isn't `str` (no injection, no corruption). No change;
covered by a regression test.

**3e. Expansion-query content read (~L1860)**:

```python
query=get_text_content(ctx.messages[-1].get("content", "")) if ctx.messages else "",
```

**3f. `_user_message` extraction for skill routing (~L2327-2328)** — replace the `str(_content)`
repr-stringify with the accessor:

```python
_content = _msg.get("content", "")
_user_message = get_text_content(_content)
```

**3g. Debug log content preview (~L2746-2755)**:

```python
messages_preview=[
    {
        "role": msg.get("role"),
        "content_preview": get_text_content(msg.get("content", ""))[:100] or None,
        "has_tool_calls": bool(msg.get("tool_calls")),
    }
    for msg in request_messages
],
```

## Step 4 — Harden `context_window.py` token estimator

**File:** `src/personal_agent/orchestrator/context_window.py`

```python
from personal_agent.llm_client.message_content import count_content_tokens

def estimate_message_tokens(message: dict[str, Any]) -> int:
    content = message.get("content", "")
    tool_calls = message.get("tool_calls")
    return max(
        1,
        count_content_tokens(content) + (estimate_tokens(str(tool_calls)) if tool_calls else 0),
    )
```

Drop the local `from personal_agent.llm_client.token_counter import estimate_tokens` import if it
becomes unused after this change — check remaining uses of `estimate_tokens` in the file (the
`tool_calls` branch still needs it, so the import stays).

`_is_tool_error_message` (~L304) and `compute_prefix_hash` (~L368-370) are left untouched:
- `_is_tool_error_message` already type-guards (`isinstance(content, str)` -> `return False`),
  matching the history_sanitiser template.
- `compute_prefix_hash` only ever hashes `messages[0]` (the system prompt), which never carries an
  attachment block in v1 scope (attachments ride the *user* turn) — not named in the ADR's site list.
  Leaving it is a deliberate scope decision, not an oversight; noted here so the PR review doesn't
  flag it as a missed site.

## Step 5 — Tests (TDD: write first, confirm failing, then implement Steps 1-4)

**New file:** `tests/personal_agent/llm_client/test_message_content.py` — unit coverage for Step 1
(see bullet list above).

**New file:** `tests/personal_agent/orchestrator/test_content_widening.py` — the AC-3 slice:

- A fixture block-list content: `[{"type": "text", "text": "look at this"}, {"type": "image_url",
  "image_url": {"url": "data:image/png;base64,AAAA"}}]`.
- `_validate_and_fix_conversation_roles`: feed two consecutive `user` messages where the second has
  block-list content -> assert the merged message's `content` is a list containing both the image
  block and the text (no string corruption, no dropped block).
- `_append_no_think_to_last_user_message`: last user message has block-list content -> assert the
  returned messages are byte-identical (unchanged) and — the bug-fix assertion — that an **earlier**
  user message (str content) in the same list is *also* unchanged (proves no fallthrough
  misapplication).
- `_append_no_think_synthesis_nudge`: `out[-1]` has block-list content -> assert unchanged.
- `_inline_volatile_into_last_user_message`: last user message has block-list content, non-empty
  `volatile_block` -> assert the returned messages equal the input (no injection attempted).
- Expansion-query read: construct `ctx.messages[-1]` with block-list content, call the same
  `get_text_content` path the executor uses -> assert result is the extracted text, not `""` and not
  a stringified block list. (Exercised at the helper level since the full `step_init` expansion
  branch requires a live controller; the accessor is already unit-tested in Step 1 — this test
  documents the call site's expectation.)
- Debug log preview: build a `request_messages` list with one block-list user message -> assert
  `content_preview` is the extracted text (or `None` for an image-only block), never a Python
  `repr([...])` string.
- `service.models.Message`: construct with `content=[...]` block list -> assert it validates and
  round-trips via `model_dump()` with the list intact (Step 2 smoke).
- `estimate_message_tokens` (context_window.py): compare token estimate for block-list content
  against `estimate_tokens(text)` for the text portion — assert the block-list estimate is **larger**
  by approximately `IMAGE_BLOCK_TOKEN_ESTIMATE` (proves image tokens are counted, not ignored via a
  cheap/wrong stringified repr) and assert it is **not** simply `estimate_tokens(str(content))` (the
  old broken behavior — a huge base64 data URI stringified would wildly overshoot the fixed
  estimate; assert the new count is close to `text_tokens + IMAGE_BLOCK_TOKEN_ESTIMATE`, not
  proportional to the base64 payload length).
- **AC-3 end-to-end assembled-request assertion** (strengthened per codex review — exercise the real
  call-bound path, not just one helper in isolation): follow the `tests/personal_agent/orchestrator/
  test_skill_injection.py` pattern — build a minimal `ExecutionContext` whose `ctx.messages` last
  entry is `{"role": "user", "content": [text block, image_url block]}`, patch
  `personal_agent.llm_client.factory.get_llm_client` to return a `MagicMock` with
  `respond = AsyncMock(return_value=<minimal LLMResponse>)`, patch
  `executor.get_default_registry` to a stub with no tools, call `await step_llm_call(ctx,
  mock_session, trace_ctx)`, then inspect `mock_llm.respond.call_args.kwargs["messages"]` — assert
  the last user message's `content` is still a `list` containing the `image_url` block, byte-for-byte
  intact (including the `data:` URI), after the real pipeline has run the no-think injection
  (skipped, per 3b) and `_validate_and_fix_conversation_roles` (a no-op for a single non-duplicate
  message) ahead of the `llm_client.respond` call. This is the actual "assembled `request_messages`"
  AC-3 refers to — the message list as it reaches `llm_client.respond`, not a synthetic proxy.

## Step 6 — Quality gates

```bash
make test-file FILE=tests/personal_agent/llm_client/test_message_content.py
make test-file FILE=tests/personal_agent/orchestrator/test_content_widening.py
make test                # full suite
make mypy
make ruff-check
make ruff-format
pre-commit run --all-files
```

## Explicitly out of scope (leave alone)

- Any real image block construction/injection (ticket 4).
- `litellm_client.py` (already block-aware; verified, no change).
- `compression_manager.py`, `context_compressor.py`, `tool_result_digest.py`, `sub_agent.py`,
  `gateway/session_api.py`, `gateway/chat_api.py`, `request_gateway/*`, `second_brain/*` — not named
  by the ADR, no live risk until an image block actually exists.
- `service/app.py` — `ctx.user_message` stays `str` (attachment metadata already travels separately
  via `ctx.attachments` since FRE-661); nothing here reads block-list content yet.

**Codex plan-review flagged (2026-07-01) as future hazards, not this ticket's job:**
`request_gateway/budget.py:53-54` (`(m.get("content") or "") + " "`),
`request_gateway/recall_controller.py:365` (`content.lower()`), and
`gateway/chat_api.py:431-434` (stringifies prior content) will crash or corrupt once list-shaped
content actually exists in persisted history (ticket 4+). File a follow-up Needs-Approval ticket
under the "Agent Vision and Attachment Ingestion" project before ticket 4 lands.
