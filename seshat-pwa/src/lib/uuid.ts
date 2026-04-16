/**
 * UUID v4 generator that works in both secure and non-secure HTTP contexts.
 *
 * `crypto.randomUUID()` is restricted to secure contexts (HTTPS / localhost).
 * When accessed over plain HTTP (e.g. WARP private network via 172.x.x.x),
 * it throws a SecurityError in Safari and Chrome.  This wrapper falls back to
 * `crypto.getRandomValues()` — which is available in all modern browsers
 * regardless of context security — to produce a cryptographically strong UUID.
 */
export function generateUUID(): string {
  if (
    typeof crypto !== 'undefined' &&
    typeof crypto.randomUUID === 'function'
  ) {
    try {
      return crypto.randomUUID();
    } catch {
      // Not in a secure context — fall through to getRandomValues polyfill.
    }
  }

  // Fallback: use crypto.getRandomValues() which is available in non-secure
  // contexts (unlike crypto.randomUUID) and is cryptographically strong.
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  // Set version (4) and variant bits per RFC 4122.
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('');
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}
