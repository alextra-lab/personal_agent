/**
 * Submit a per-turn 0–3 value rating to the Seshat backend (FRE-407).
 *
 * Writes to POST /api/v1/turns/{traceId}/rating. The endpoint is idempotent —
 * a re-rating for the same trace_id overwrites the single ES document. The
 * backend resolves prompt identity from the ES telemetry log and persists the
 * rating to `user-turn-ratings-*`.
 *
 * Rating scale:
 *   0 — No value
 *   1 — Low value
 *   2 — Meets expectation
 *   3 — Wow
 */

import { SESHAT_API, authHeaders } from './agui-client';

/**
 * Post a user turn rating to the backend.
 *
 * Never throws — failures are returned as `false` so callers can revert
 * optimistic state without crashing the chat.
 *
 * @param traceId   - The turn's trace_id (join key).
 * @param sessionId - The session that owns the turn (security ownership check).
 * @param rating    - Integer in [0, 3].
 * @returns True when the backend acknowledged; false on any error.
 */
export async function submitTurnRating(
  traceId: string,
  sessionId: string,
  rating: number,
): Promise<boolean> {
  try {
    const resp = await fetch(
      `${SESHAT_API}/api/v1/turns/${encodeURIComponent(traceId)}/rating`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify({ rating, session_id: sessionId }),
      },
    );
    return resp.ok;
  } catch {
    // Network error or fetch API unavailable — silent fail; caller reverts.
    return false;
  }
}
