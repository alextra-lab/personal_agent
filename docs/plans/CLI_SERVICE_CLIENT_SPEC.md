# CLI Service Client Spec

**Date**: 2026-02-22  
**Status**: Draft  
**Phase**: 2.6 Conversational Agent MVP  
**Depends on**: `CONVERSATION_CONTINUITY_SPEC.md` (session hydration + context window)  
**Related**: Service API (`docs/architecture/SERVICE_IMPLEMENTATION_SPEC_v0.1.md`), `docs/USAGE_GUIDE.md`

---

## Purpose

Provide a terminal-friendly command-line script that talks to the Personal Agent **service** over HTTP(S), enabling real multi-turn conversations without manual `curl` or session ID handling. The script manages session lifecycle so subsequent messages reuse the same conversation — and once the backend hydrates session context (see `CONVERSATION_CONTINUITY_SPEC.md`), the agent sees the full conversation history and can collaborate as a true thinking partner.

---

## Scope

### In Scope (MVP)

- **Chat command**: Send a message to the service `POST /chat`; create or reuse session; print reply.
- **Session persistence**: Store current session ID in a well-known file so subsequent invocations reuse the same conversation.
- **Configurable base URL**: Use `AGENT_SERVICE_URL` (or project config) so users can point at `http://localhost:9000` (dev) or `https://...` (production).
- **New conversation**: Flag (e.g. `--new`) to create a new session and replace the stored session ID.
- **Session subcommand** (minimal): Show current session ID; optionally create new and set as current.
- **Entrypoint**: Runnable via `uv run agent ...` or `uv run python -m personal_agent.ui.service_cli ...` with a clear, documented command shape.
- **Structured errors**: Clear messages when service is unreachable or returns errors; no raw stack traces for connection/HTTP errors.

### Out of Scope (MVP)

- Interactive REPL mode (type multiple messages in one process).
- Telemetry `query` / `trace` in the same script (can be added later or kept in existing `personal_agent.ui.cli`).
- Authentication (API keys, tokens) — assume local or trusted network for MVP.
- TLS client certificates or custom CA handling beyond standard `httpx` behavior.

---

## Design

### 1) Base URL and TLS

- **Configurable base URL**: Env var `AGENT_SERVICE_URL` (default `http://localhost:9000`). No hardcoded `http://` or `https://`; user chooses scheme.
- **TLS**: For production, users set `AGENT_SERVICE_URL=https://agent.example.com`. The client uses `httpx`; certificate verification follows default behavior (verify for HTTPS). No custom TLS logic in MVP.

### 2) Session file

- **Location**: `config/current_session` under project root, or `~/.config/personal_agent/current_session` if config dir exists and is writable. Prefer project-local for dev, XDG for installed use.
- **Content**: Single line = current session UUID. Overwrite on `--new` or when creating first session.
- **Behavior**: If file exists and contains a valid UUID, use it for `POST /chat?message=...&session_id=...`. If not, create session via `POST /sessions`, then write session ID to file and use it for chat.

### 3) Commands and options

| Command / option | Description |
|------------------|-------------|
| `agent "message"` | Send message to agent (create or reuse session); print reply. |
| `agent chat "message"` | Same as above (explicit subcommand). |
| `agent chat "message" --new` | Create new session, send message, update session file. |
| `agent session` | Print current session ID (from session file). |
| `agent session new` | Create new session via API, write to session file, print session ID. |
| `agent --help` | Show usage. |

All requests go to `{AGENT_SERVICE_URL}/sessions` and `{AGENT_SERVICE_URL}/chat`.

### 4) Request/response handling

- **Create session**: `POST {base}/sessions` with body `{"channel": "CLI", "mode": "NORMAL"}`. Parse `session_id` from JSON response.
- **Chat**: `POST {base}/chat?message={urlencoded_message}&session_id={session_id}` (or omit `session_id` to let service create one; then persist returned `session_id` in session file for next time).
- **Output**: Print assistant reply (e.g. `result["response"]`) with optional trace ID for debugging. Use Rich for formatted output if available, else plain text.
- **Errors**: On connection error or non-2xx: print user-friendly message and exit non-zero. Include error reference or status code; no raw tracebacks for HTTP/connection errors.

### 5) Message encoding

- Service accepts `message` as query parameter. For long messages, the script must URL-encode the body. Use `urllib.parse.quote` (or httpx's built-in) so multi-line and special characters work.

### 6) File layout (suggested)

- **New module**: `src/personal_agent/ui/service_cli.py` — Typer app that:
  - Reads `AGENT_SERVICE_URL` from env (or `personal_agent.config` if unified settings expose it).
  - Implements `chat` and `session` commands; uses httpx for HTTP.
  - Reads/writes session file via a small helper (path in config or constant).
- **Entrypoint**: In `pyproject.toml`, add script entry e.g. `agent = "personal_agent.ui.service_cli:app"` so `uv run agent "..."` works.
- **Docs**: Update `docs/USAGE_GUIDE.md` with "Using the agent from the terminal" section that describes `agent "message"`, `--new`, `agent session`, and `AGENT_SERVICE_URL`.

### 7) Backend dependency (conversation continuity)

- **Prerequisite**: `CONVERSATION_CONTINUITY_SPEC.md` must be implemented first. That spec adds session hydration (loading DB messages into the orchestrator) and context window management. Without it, the CLI can send `session_id` but the agent won't see prior turns.
- The CLI is designed so that once the backend hydrates session context, multi-turn conversations work end-to-end without any CLI changes.

---

## Acceptance criteria

- [ ] `uv run agent "Hello"` creates a session (if none), POSTs to `/chat`, prints reply, and stores session ID.
- [ ] Second run `uv run agent "Follow up"` reuses stored session ID and sends it to `/chat`.
- [ ] `uv run agent chat "Hi" --new` creates a new session, sends message, overwrites session file.
- [ ] `uv run agent session` prints current session ID (or "No session" if file missing/empty).
- [ ] `uv run agent session new` creates session via API, writes to session file, prints ID.
- [ ] With `AGENT_SERVICE_URL=https://...` (or `http://localhost:9000`), all requests use that base; no hardcoded scheme/host.
- [ ] On connection failure or 4xx/5xx, script prints a clear error and exits with non-zero status.
- [ ] Long and multi-line messages are sent correctly (URL-encoded).
- [ ] `docs/USAGE_GUIDE.md` documents the script, env var, and session behavior.

---

## References

- Conversation continuity: `docs/plans/CONVERSATION_CONTINUITY_SPEC.md` (prerequisite).
- Service API: `docs/architecture/SERVICE_IMPLEMENTATION_SPEC_v0.1.md` (sessions, chat).
- Existing in-process CLI: `src/personal_agent/ui/cli.py` (chat, telemetry query/trace).
- Config: `personal_agent.config.settings` (optional: add `agent_service_url` if desired; else env-only for MVP).
