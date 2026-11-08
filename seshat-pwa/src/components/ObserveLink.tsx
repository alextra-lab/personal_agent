'use client';

import { useObserveUrl } from '@/components/RuntimeConfigProvider';

/** Returns the address when it is an absolute http(s) URL, else null. */
function safeHttpUrl(raw: string): string | null {
  try {
    const url = new URL(raw);
    return url.protocol === 'https:' || url.protocol === 'http:' ? raw : null;
  } catch {
    return null;
  }
}

/**
 * Drawer link that opens Observe (Grafana) in a new tab (FRE-1754).
 *
 * The address comes from the runtime config (OBSERVE_URL), so the public repo
 * holds no domain. The link does not render when no address is configured or
 * when the address is not an absolute http(s) URL.
 */
export function ObserveLink({ onClick }: { onClick?: () => void }) {
  const href = safeHttpUrl(useObserveUrl());
  if (!href) return null;

  return (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      onClick={onClick}
      className="flex items-center gap-2 px-4 py-2.5 text-sm text-ink-muted hover:text-ink hover:bg-surface border-b border-line/50 transition-colors"
    >
      <span aria-hidden="true">📊</span>
      Observe (Grafana)
      <span aria-hidden="true" className="ml-auto">↗</span>
    </a>
  );
}
