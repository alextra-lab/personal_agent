/**
 * Low-level AG-UI client utilities.
 *
 * Provides helpers for interacting with the Seshat backend:
 * - Sending chat messages via POST /chat/stream
 * - Connecting to the AG-UI SSE stream at GET /stream/{session_id}
 * - Resuming HITL interrupts via POST /stream/{session_id}/resume
 * - Submitting tool-approval decisions via POST /approval/{request_id}
 *
 * All requests include an Authorization header when NEXT_PUBLIC_GATEWAY_TOKEN
 * is set (production). In local dev (token absent) the header is omitted and
 * the gateway's auth-disabled fast-path allows the request through.
 */

import { fetchEventSource } from '@microsoft/fetch-event-source';

import type { AGUIEvent } from './types';

/** Base URL for the Seshat backend. Defaults to localhost in dev. */
export const SESHAT_API =
  process.env.NEXT_PUBLIC_SESHAT_URL ?? 'http://localhost:9000';

/**
 * Bearer token for gateway authentication.
 * Baked into the bundle at build time via NEXT_PUBLIC_GATEWAY_TOKEN.
 * Empty string in local dev — gateway auth is disabled locally.
 */
const GATEWAY_TOKEN = process.env.NEXT_PUBLIC_GATEWAY_TOKEN ?? '';

/** Returns auth headers when a token is configured; empty object otherwise. */
function authHeaders(): Record<string, string> {
  return GATEWAY_TOKEN ? { Authorization: `Bearer ${GATEWAY_TOKEN}` } : {};
}

// --------------------------------------------------------------------------
// Chat message dispatch
// --------------------------------------------------------------------------

export interface SendMessageOptions {
  message: string;
  sessionId: string;
  profile?: string;
}

/**
 * Send a chat message to the Seshat backend.
 *
 * Uses form-encoded body to match the existing FastAPI /chat endpoint.
 * The backend emits events to the SSE stream identified by sessionId.
 *
 * @throws Error when the backend returns a non-2xx status.
 */
export async function sendChatMessage(opts: SendMessageOptions): Promise<void> {
  const { message, sessionId, profile = 'local' } = opts;

  const resp = await fetch(`${SESHAT_API}/chat/stream`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/x-www-form-urlencoded',
      ...authHeaders(),
    },
    body: new URLSearchParams({
      message,
      session_id: sessionId,
      profile,
    }),
  });

  if (!resp.ok) {
    throw new Error(`Seshat /chat/stream returned ${resp.status}: ${resp.statusText}`);
  }
}

// --------------------------------------------------------------------------
// SSE stream connection
// --------------------------------------------------------------------------

export type AGUIEventHandler = (event: AGUIEvent) => void;
export type ErrorHandler = (error: Event) => void;

export interface StreamConnection {
  /** Close the stream and stop receiving events. */
  close: () => void;
}

/**
 * Connect to the AG-UI SSE stream for a session.
 *
 * Uses fetch-event-source instead of the browser's EventSource so that
 * an Authorization header can be included. The StreamConnection interface
 * is identical to the previous EventSource-based implementation —
 * useSSEStream.ts needs no changes.
 *
 * Reconnection on transient network failures is handled automatically by
 * fetchEventSource. Deliberate close (via StreamConnection.close) and
 * server-side errors abort without retrying.
 *
 * @param sessionId - Target session to stream.
 * @param onEvent   - Called for each AG-UI event received.
 * @param onError   - Called on stream error (before close).
 * @returns StreamConnection with a close() method.
 */
export function connectToStream(
  sessionId: string,
  onEvent: AGUIEventHandler,
  onError?: ErrorHandler,
): StreamConnection {
  const ctrl = new AbortController();

  fetchEventSource(`${SESHAT_API}/stream/${encodeURIComponent(sessionId)}`, {
    headers: authHeaders(),
    signal: ctrl.signal,
    onmessage(ev) {
      try {
        const parsed = JSON.parse(ev.data) as AGUIEvent;
        onEvent(parsed);
      } catch {
        // Malformed event — skip silently; structlog on backend will have trace.
      }
    },
    onerror(err) {
      if (onError) onError(new Event('error'));
      // Rethrow to stop fetchEventSource's retry loop on errors.
      throw err;
    },
  }).catch(() => {
    // Swallow AbortError from ctrl.abort() and any onerror rethrows —
    // they are already surfaced to the caller via onError.
  });

  return {
    close: () => ctrl.abort(),
  };
}

// --------------------------------------------------------------------------
// HITL resume
// --------------------------------------------------------------------------

export interface ResumeOptions {
  sessionId: string;
  choice: string;
}

/**
 * Resume a HITL-interrupted session with the user's choice.
 *
 * The backend expects a POST to /stream/{session_id}/resume with a JSON body
 * containing the chosen option.
 *
 * @throws Error when the backend returns a non-2xx status.
 */
export async function resumeInterrupt(opts: ResumeOptions): Promise<void> {
  const { sessionId, choice } = opts;

  const resp = await fetch(
    `${SESHAT_API}/stream/${encodeURIComponent(sessionId)}/resume`,
    {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        ...authHeaders(),
      },
      body: JSON.stringify({ choice }),
    },
  );

  if (!resp.ok) {
    throw new Error(`Resume failed: ${resp.status} ${resp.statusText}`);
  }
}

// --------------------------------------------------------------------------
// Tool approval decisions
// --------------------------------------------------------------------------

/**
 * Submit an approve or deny decision for a pending tool-approval request.
 *
 * The backend endpoint is ``POST /approval/{request_id}``.  The agent is
 * blocking on this call and will proceed (or abort) once the decision lands.
 *
 * @param sessionId  - Current session (unused in the URL, kept for symmetry with resumeInterrupt).
 * @param requestId  - The ``request_id`` from the ``tool_approval_request`` SSE event.
 * @param decision   - ``'approve'`` or ``'deny'``.
 * @param reason     - Optional free-text rationale shown in backend logs.
 * @throws Error when the backend returns a non-2xx status.
 */
export async function postApprovalDecision(
  // _sessionId is accepted for call-site symmetry with resumeInterrupt but is
  // not used in the URL — the backend derives ownership from the auth token.
  _sessionId: string,
  requestId: string,
  decision: 'approve' | 'deny',
  reason?: string,
): Promise<void> {

  const body: Record<string, unknown> = { decision };
  if (reason !== undefined) {
    body['reason'] = reason;
  }

  const resp = await fetch(
    `${SESHAT_API}/approval/${encodeURIComponent(requestId)}`,
    {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        ...authHeaders(),
      },
      body: JSON.stringify(body),
    },
  );

  if (!resp.ok) {
    throw new Error(`postApprovalDecision failed: ${resp.status} ${resp.statusText}`);
  }
}

// --------------------------------------------------------------------------
// Session history
// --------------------------------------------------------------------------

/** Summary of a persisted session from GET /api/v1/sessions. */
export interface SessionSummary {
  session_id: string;
  created_at: string;
  last_active_at: string;
  mode: string;
  channel: string | null;
  message_count: number;
  title: string | null;
}

/** A single persisted message from GET /api/v1/sessions/{id}/messages. */
export interface ServerMessage {
  role: string;
  content: string;
  timestamp?: string;
  trace_id?: string;
  metadata?: Record<string, unknown>;
}

/**
 * List recent sessions from the backend.
 *
 * @param limit - Maximum number of sessions to return (default 20).
 * @returns Array of session summaries, most-recent first.
 * @throws Error when the backend returns a non-2xx status.
 */
export async function listSessions(limit = 20): Promise<SessionSummary[]> {
  const resp = await fetch(
    `${SESHAT_API}/api/v1/sessions?limit=${limit}`,
    { headers: authHeaders() },
  );
  if (!resp.ok) throw new Error(`listSessions failed: ${resp.status}`);
  return resp.json() as Promise<SessionSummary[]>;
}

/**
 * Fetch the message history for a session.
 *
 * Returns an empty array when the session does not exist (404) so callers
 * can treat it as a fresh session without special-casing.
 *
 * @param sessionId - The session to fetch messages for.
 * @param limit     - Maximum number of messages to return (default 200).
 * @returns Array of server messages in chronological order.
 * @throws Error when the backend returns a non-2xx, non-404 status.
 */
export async function getSessionMessages(
  sessionId: string,
  limit = 200,
): Promise<ServerMessage[]> {
  const resp = await fetch(
    `${SESHAT_API}/api/v1/sessions/${encodeURIComponent(sessionId)}/messages?limit=${limit}`,
    { headers: authHeaders() },
  );
  if (resp.status === 404) return [];
  if (!resp.ok) throw new Error(`getSessionMessages failed: ${resp.status}`);
  return resp.json() as Promise<ServerMessage[]>;
}
