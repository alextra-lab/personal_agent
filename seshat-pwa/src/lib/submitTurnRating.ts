/**
 * Submit a per-turn 0–3 value rating to the Seshat backend (FRE-407).
 *
 * Writes to POST /api/v1/turns/{traceId}/rating. The endpoint is idempotent —
 * an explicit re-rating for the same trace_id overwrites the single ES
 * document. The backend resolves prompt identity from the ES telemetry log and
 * persists the rating to `user-turn-ratings-*`.
 *
 * Rating scale (store, FRE-407): 0 error · 1 low (legacy) · 2 ok · 3 exceptional.
 *
 * A `default` write (FRE-757 "persist ok on send") is create-if-absent: the
 * backend never overwrites an existing explicit rating with it and does not
 * emit the rating bus event.
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
 * @param isDefault - When true, sends a create-if-absent "persist ok on send"
 *                    write (FRE-757) that never overwrites an explicit rating.
 * @returns True when the backend acknowledged; false on any error.
 */
export async function submitTurnRating(
  traceId: string,
  sessionId: string,
  rating: number,
  isDefault = false,
): Promise<boolean> {
  try {
    const resp = await fetch(
      `${SESHAT_API}/api/v1/turns/${encodeURIComponent(traceId)}/rating`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify({ rating, session_id: sessionId, default: isDefault }),
      },
    );
    return resp.ok;
  } catch {
    // Network error or fetch API unavailable — silent fail; caller reverts.
    return false;
  }
}
