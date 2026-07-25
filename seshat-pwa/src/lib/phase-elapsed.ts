/**
 * Pure elapsed-time math for the live phase surface (ADR-0123 T3, FRE-936).
 *
 * `now` is an explicit parameter (defaulting to `Date.now()`) so callers can
 * pin it in tests and so a reconnect never resets elapsed to zero — it is
 * always recomputed from the server's `started_at` timestamp, never from
 * when the client happened to (re)connect.
 */

/** Elapsed milliseconds from a server ISO-8601 timestamp to `now`. Never negative. */
export function elapsedMs(startedAtIso: string, now: number = Date.now()): number {
  return Math.max(0, now - new Date(startedAtIso).getTime());
}

/** Formats elapsed milliseconds as `"5s"` or `"2m 10s"`. */
export function formatElapsed(ms: number): string {
  const totalSeconds = Math.floor(ms / 1000);
  if (totalSeconds < 60) return `${totalSeconds}s`;
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return `${minutes}m ${seconds.toString().padStart(2, '0')}s`;
}

/**
 * Past this many elapsed ms, the phase surface adds static "escalating
 * candour" context (e.g. "Large artifacts can take several minutes") rather
 * than just counting — ADR-0123 §3. Never a progress bar or percentage.
 */
export const ESCALATION_THRESHOLD_MS = 60_000;
