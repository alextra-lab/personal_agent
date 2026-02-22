# Conversation Continuity Spec

**Date**: 2026-02-22
**Status**: Proposed
**Phase**: 2.6 Conversational Agent MVP
**Related**: ADR-0018 (Seshat), Architecture Assessment 2026-02-22, `CLI_SERVICE_CLIENT_SPEC.md`

---

## Purpose

Enable true multi-turn conversations through the Personal Agent service. Today the service persists messages in PostgreSQL but creates a fresh in-memory orchestrator per request, so the model never sees prior turns. This is the **foundational gap** blocking the project's core thesis: a collaborator that gains knowledge along the journey.

Without conversation continuity:
- The agent cannot reference what was discussed earlier in the same session.
- The Captain's Log reflects on isolated single-turn traces, not rich conversations.
- The memory graph (Neo4j) receives shallow, disconnected conversation nodes.
- Seshat (ADR-0018) would curate an impoverished knowledge base.

**Conversation continuity is not a feature — it is the prerequisite for the entire memory and learning architecture.**

---

## Scope

### In Scope

1. **Session hydration**: Load conversation history from PostgreSQL into the orchestrator's in-memory session before each request.
2. **Context window management**: Prevent prompt overflow by applying a message budget (token-aware truncation with optional summarization).
3. **Session-scoped orchestrator**: Reuse or properly initialize the orchestrator so it has the session's full message history available to `step_init`.
4. **Configuration**: New settings for max conversation tokens, summarization toggle, and message retention window.
5. **Observability**: Log context window usage (messages loaded, tokens used, messages truncated) per request.

### Out of Scope

- Conversation summarization via LLM (deferred to Seshat; use simple truncation for MVP).
- Cross-session context (handled by memory graph / Seshat).
- Session expiration or archival policies (future).
- Changes to the CLI service client script (separate spec, `CLI_SERVICE_CLIENT_SPEC.md`).

---

## Design

### 1) Session hydration in `/chat` handler

**File**: `src/personal_agent/service/app.py`

The `/chat` endpoint currently creates a new `Orchestrator()` per request and creates an empty in-memory session. The fix: after creating the orchestrator session, load the DB session's message history into it.

```python
# After creating orchestrator session (line ~275-277):
session_manager.create_session(
    Mode.NORMAL, Channel.CHAT, session_id=str(session.session_id)
)

# NEW: Hydrate from DB — load prior messages into orchestrator session
db_messages = session.messages or []
if db_messages:
    session_manager.update_session(
        str(session.session_id), messages=db_messages
    )
```

This is the critical change. Once the in-memory session has messages, `step_init` (line 776-778 of `executor.py`) already does `ctx.messages = list(session.messages)`, so the LLM will see the full conversation history.

**Important**: The current user message is appended to the DB *before* the orchestrator call (line 259). So `db_messages` already includes the current user message. However, `step_init` also appends the user message (line 781). To avoid duplication, either:
- (a) Load DB messages **excluding** the just-appended current message, or
- (b) Load DB messages **before** the `append_message` call, or
- (c) Skip the `step_init` append when messages are pre-loaded.

**Recommended**: Option (b) — load DB messages before appending the current user message. This keeps `step_init` logic unchanged.

### 2) Context window management

**File**: `src/personal_agent/orchestrator/context_window.py` (new)

As conversations grow, the full message history will exceed the model's context window. Apply a **token budget** to the conversation history before sending to the LLM.

Strategy (MVP — no LLM summarization):

1. Reserve tokens for system prompt (~500), tools (~2000), and response (~2000).
2. Remaining budget = model context window - reserved tokens.
3. Always keep the **first message** (system prompt / session opener) and the **last N messages** (recent context).
4. If history exceeds budget, truncate from the middle (oldest non-system messages first).
5. Insert a `[Earlier messages truncated]` marker so the model knows context was trimmed.

```python
def apply_context_window(
    messages: list[dict[str, Any]],
    max_tokens: int,
    reserved_tokens: int = 4500,
) -> list[dict[str, Any]]:
    """Trim conversation history to fit within token budget.

    Keeps first message (system/opener) and most recent messages.
    Truncates from the middle when history exceeds budget.

    Args:
        messages: Full message history (OpenAI format).
        max_tokens: Model's total context window size.
        reserved_tokens: Tokens reserved for system prompt, tools, response.

    Returns:
        Trimmed message list that fits within budget.
    """
```

Token counting: Use a simple heuristic (chars / 4) for MVP. Can upgrade to tiktoken or model-specific tokenizer later.

### 3) Configuration

**File**: `src/personal_agent/config/settings.py`

New fields on `AppConfig`:

```python
# Conversation continuity
conversation_max_history_messages: int = Field(
    default=50,
    ge=1,
    description="Maximum number of messages to load from DB into orchestrator session",
)
conversation_max_context_tokens: int = Field(
    default=6000,
    ge=500,
    description="Token budget for conversation history in LLM prompt (excluding system prompt and tools)",
)
conversation_context_strategy: str = Field(
    default="truncate",
    description="Context window strategy: 'truncate' (drop oldest) or 'summarize' (future: LLM summary)",
)
```

### 4) Integration with step_init

**File**: `src/personal_agent/orchestrator/executor.py`

In `step_init`, after loading session messages and before building the LLM prompt, apply context window management:

```python
# Load session and build message history
session = session_manager.get_session(ctx.session_id)
if session:
    ctx.messages = list(session.messages)

# Add new user message
ctx.messages.append({"role": "user", "content": ctx.user_message})

# NEW: Apply context window management
from personal_agent.orchestrator.context_window import apply_context_window
ctx.messages = apply_context_window(
    ctx.messages,
    max_tokens=settings.conversation_max_context_tokens,
)
```

### 5) Observability

Log per-request context metrics:

```python
log.info(
    "conversation_context_loaded",
    trace_id=ctx.trace_id,
    session_id=ctx.session_id,
    total_messages_in_db=len(db_messages),
    messages_loaded=len(ctx.messages),
    messages_truncated=max(0, len(db_messages) - len(ctx.messages)),
    estimated_tokens=estimated_token_count,
)
```

---

## Files to create/modify

| Action | File | Description |
|--------|------|-------------|
| **Modify** | `src/personal_agent/service/app.py` | Hydrate orchestrator session from DB messages |
| **Create** | `src/personal_agent/orchestrator/context_window.py` | Token-aware message truncation |
| **Modify** | `src/personal_agent/orchestrator/executor.py` | Apply context window in `step_init` |
| **Modify** | `src/personal_agent/config/settings.py` | Add conversation config fields |
| **Create** | `tests/test_orchestrator/test_context_window.py` | Unit tests for truncation logic |
| **Modify** | `tests/test_orchestrator/` | Integration test: multi-turn via service |

---

## Acceptance criteria

- [ ] Sending two messages with the same `session_id` to `POST /chat` results in the second response being aware of the first message (model sees conversation history).
- [ ] Conversation history loaded from DB matches what was persisted (no duplication, correct order).
- [ ] Context window management prevents prompt overflow: a 100-message conversation is truncated to fit token budget.
- [ ] Truncation preserves the most recent messages and inserts a marker for dropped messages.
- [ ] New config fields (`conversation_max_history_messages`, `conversation_max_context_tokens`) are respected.
- [ ] Telemetry logs context loading metrics (messages loaded, truncated, estimated tokens).
- [ ] Existing single-turn behavior is unchanged (no regression).
- [ ] Unit tests for `apply_context_window` cover: short history (no truncation), long history (truncation), edge cases (empty, single message).

---

## Connection to Seshat and the learning pipeline

This spec enables the **working memory** layer in Seshat's memory taxonomy (ADR-0018):

```
Working Memory (THIS SPEC — session history, multi-turn)
  → Episodic Memory (Seshat indexes rich conversations)
    → Semantic Memory (Seshat promotes validated facts)
      → Derived Memory (Seshat synthesizes patterns)
        → Profile Memory (Seshat learns preferences)
```

Without this spec, the entire pipeline is starved. With it, every conversation generates the rich trace data that Captain's Log, Second Brain, and eventually Seshat need to make the agent intelligent over time.

---

## References

- Vision: `docs/VISION_DOC.md` — "Partnership Over Servitude," "Context stewardship"
- Architecture Assessment: `docs/research/ARCHITECTURE_ASSESSMENT_2026-02-22.md` — Section 4 (Seshat), Section 2 (gaps)
- ADR-0018: `docs/architecture_decisions/ADR-0018-seshat-memory-librarian-agent.md` — Memory type taxonomy
- Service spec: `docs/architecture/SERVICE_IMPLEMENTATION_SPEC_v0.1.md` — Session and chat endpoints
- Orchestrator: `src/personal_agent/orchestrator/executor.py` — `step_init` (lines 755-861)
- Service handler: `src/personal_agent/service/app.py` — `/chat` endpoint (lines 230-334)
