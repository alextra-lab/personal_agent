/**
 * SharedLinksView — the caller's active share links, with Revoke (FRE-1784).
 *
 * Implements the Shared links view of ADR-0153 Amendment 1 L9. It lists
 * `GET /api/v1/share-links` (the caller's own links only) and revokes one with
 * `DELETE /api/v1/share-links/{link_id}`. The list holds no URL and no token.
 */

'use client';

import { useEffect, useState } from 'react';
import Link from 'next/link';

import { ShareLinkError, listShareLinks, revokeShareLink } from '@/lib/agui-client';
import type { ShareLinkSummary } from '@/lib/agui-client';

type ListState =
  | { phase: 'loading' }
  | { phase: 'failed'; message: string }
  | { phase: 'loaded'; links: ShareLinkSummary[] };

function formatTime(iso: string): string {
  return new Date(iso).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' });
}

function errorText(err: unknown): string {
  if (err instanceof ShareLinkError && err.status === 403) {
    return 'Only a link creator can see shared links.';
  }
  return err instanceof Error ? err.message : 'Could not load shared links.';
}

/** The Shared links view for the /shared-links route. */
export function SharedLinksView() {
  const [state, setState] = useState<ListState>({ phase: 'loading' });
  const [revoking, setRevoking] = useState<ReadonlySet<string>>(new Set());
  const [rowErrors, setRowErrors] = useState<Readonly<Record<string, string>>>({});

  useEffect(() => {
    let cancelled = false;
    listShareLinks()
      .then((links) => {
        if (!cancelled) setState({ phase: 'loaded', links });
      })
      .catch((err: unknown) => {
        if (!cancelled) setState({ phase: 'failed', message: errorText(err) });
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const handleRevoke = async (linkId: string) => {
    if (revoking.has(linkId)) return;
    setRevoking((prev) => new Set(prev).add(linkId));
    setRowErrors(({ [linkId]: _dropped, ...rest }) => rest);
    try {
      try {
        await revokeShareLink(linkId);
      } catch (err) {
        // 404: the caller has no link with this id any more, so the row is stale. Drop it.
        if (!(err instanceof ShareLinkError && err.status === 404)) throw err;
      }
      setState((prev) =>
        prev.phase === 'loaded'
          ? { phase: 'loaded', links: prev.links.filter((l) => l.link_id !== linkId) }
          : prev,
      );
    } catch (err) {
      setRowErrors((prev) => ({
        ...prev,
        [linkId]: err instanceof Error ? err.message : 'Could not revoke the link.',
      }));
    } finally {
      setRevoking((prev) => {
        const next = new Set(prev);
        next.delete(linkId);
        return next;
      });
    }
  };

  return (
    <div className="min-h-full bg-bg text-ink flex flex-col">
      <header
        className="flex items-center gap-3 px-4 border-b border-line bg-bg/80 backdrop-blur-sm flex-shrink-0"
        style={{ paddingTop: 'calc(env(safe-area-inset-top, 0px) + 0.75rem)', paddingBottom: '0.75rem' }}
      >
        <Link
          href="/artifacts"
          className="flex items-center gap-1 text-sm text-ink-muted hover:text-ink transition-colors"
        >
          ← Artifacts
        </Link>
        <span className="text-line" aria-hidden="true">|</span>
        <h1 className="text-sm font-semibold text-ink">Shared links</h1>
      </header>

      <main className="flex-1 overflow-y-auto">
        {state.phase === 'loading' && (
          <p className="px-4 py-6 text-sm text-ink-muted">Loading…</p>
        )}

        {state.phase === 'failed' && (
          <p role="alert" className="px-4 py-6 text-sm text-red-700 dark:text-red-400">
            {state.message}
          </p>
        )}

        {state.phase === 'loaded' && state.links.length === 0 && (
          <p className="px-4 py-24 text-center text-sm text-ink-muted">No active links.</p>
        )}

        {state.phase === 'loaded' && state.links.length > 0 && (
          <ul className="divide-y divide-line/60">
            {state.links.map((link) => (
              <li key={link.link_id} className="flex items-center gap-3 px-4 py-2.5">
                <div className="flex-1 min-w-0 flex flex-col gap-0.5">
                  <span className="text-sm font-medium text-ink truncate">
                    {link.artifact_title ?? 'Untitled'}
                  </span>
                  <span className="text-xs text-ink-muted">
                    <span>Expires {formatTime(link.expires_at)}</span>
                    {' · '}
                    <span>{link.view_count === 1 ? '1 view' : `${link.view_count} views`}</span>
                    {' · '}
                    <span>
                      Last view: {link.last_viewed_at ? formatTime(link.last_viewed_at) : 'never'}
                    </span>
                  </span>
                  {rowErrors[link.link_id] && (
                    <span role="alert" className="text-xs text-red-700 dark:text-red-400">
                      {rowErrors[link.link_id]}
                    </span>
                  )}
                </div>
                <button
                  onClick={() => handleRevoke(link.link_id)}
                  disabled={revoking.has(link.link_id)}
                  className="flex-shrink-0 px-3 py-1 rounded-lg text-xs font-semibold border border-red-600 text-red-700 dark:text-red-300 hover:bg-red-50 dark:hover:bg-red-900/30 disabled:opacity-60 transition-colors"
                >
                  Revoke
                </button>
              </li>
            ))}
          </ul>
        )}
      </main>
    </div>
  );
}
