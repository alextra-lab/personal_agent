/**
 * Low-level AG-UI client utilities.
 *
 * Provides helpers for interacting with the Seshat backend:
 * - Sending chat messages via POST /chat
 * - Connecting to the AG-UI SSE stream at GET /stream/{session_id}
 * - Resuming HITL interrupts via POST /stream/{session_id}/resume
 *
 * The useSSEStream hook composes these for React component use.
 */

import type { AGUIEvent } from './types';

/** Base URL for the Seshat backend. Defaults to localhost in dev. */
export const SESHAT_API =
  process.env.NEXT_PUBLIC_SESHAT_URL ?? 'http://localhost:9000';

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
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
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
  /** Close the EventSource and stop receiving events. */
  close: () => void;
}

/**
 * Connect to the AG-UI SSE stream for a session.
 *
 * The EventSource is created, handlers registered, and a close handle
 * returned so the caller can tear down the connection.
 *
 * @param sessionId - Target session to stream.
 * @param onEvent   - Called for each AG-UI event received.
 * @param onError   - Called on EventSource error (before close).
 * @returns StreamConnection with a close() method.
 */
export function connectToStream(
  sessionId: string,
  onEvent: AGUIEventHandler,
  onError?: ErrorHandler,
): StreamConnection {
  const url = `${SESHAT_API}/stream/${encodeURIComponent(sessionId)}`;
  const es = new EventSource(url);

  es.onmessage = (rawEvent: MessageEvent<string>) => {
    try {
      const parsed = JSON.parse(rawEvent.data) as AGUIEvent;
      onEvent(parsed);
    } catch {
      // Malformed event — skip silently; structlog on backend will have trace.
    }
  };

  if (onError) {
    es.onerror = onError;
  }

  return {
    close: () => es.close(),
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
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ choice }),
    },
  );

  if (!resp.ok) {
    throw new Error(`Resume failed: ${resp.status} ${resp.statusText}`);
  }
}
