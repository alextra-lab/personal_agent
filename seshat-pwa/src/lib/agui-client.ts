/**
 * Low-level AG-UI client utilities.
 *
 * Provides helpers for interacting with the Seshat backend:
 * - Sending chat messages via POST /chat/stream
 * - Connecting to the AG-UI SSE stream at GET /stream/{session_id}
 * - Resuming HITL interrupts via POST /stream/{session_id}/resume
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
