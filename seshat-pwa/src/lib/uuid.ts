/**
 * UUID v4 generator that works in non-secure HTTP contexts.
 *
 * `crypto.randomUUID()` is restricted to secure contexts (HTTPS / localhost).
 * When accessed over plain HTTP (e.g. WARP private network via 172.x.x.x),
 * it throws a SecurityError in Safari and Chrome.  This wrapper falls back to
 * a Math.random-based v4 UUID which is sufficient for client-side session IDs.
 */
export function generateUUID(): string {
  if (
    typeof crypto !== 'undefined' &&
    typeof crypto.randomUUID === 'function'
  ) {
    try {
      return crypto.randomUUID();
    } catch {
      // Not in a secure context — fall through to polyfill.
    }
  }

  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (c) => {
    const r = (Math.random() * 16) | 0;
    const v = c === 'x' ? r : (r & 0x3) | 0x8;
    return v.toString(16);
  });
}
