import { listSessions } from './agui-client';

/** localStorage key holding the last-active session ID (written by StreamingChat). */
export const LAST_SESSION_KEY = 'seshat_last_session_id';

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export function isValidUUID(s: string): boolean {
  return UUID_RE.test(s);
}

/**
 * Resolve the session a returning visitor should land on.
 *
 * Checks localStorage first — no network call, the common case. Only when
 * that key is absent or malformed does it ask the server for the most
 * recent session, so a cleared-storage visitor with existing history
 * resumes there instead of losing the reference (FRE-938).
 *
 * @returns The resolved session ID, or `undefined` when there is genuinely
 *   no session to resume (a brand-new user, or the server lookup failed).
 */
export async function resolveLastSessionId(): Promise<string | undefined> {
  const stored = localStorage.getItem(LAST_SESSION_KEY);
  if (stored && isValidUUID(stored)) return stored;

  try {
    const sessions = await listSessions(1);
    return sessions[0]?.session_id;
  } catch {
    return undefined;
  }
}
