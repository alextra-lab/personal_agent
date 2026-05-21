/**
 * ArtifactsIndex — client component for the /artifacts route (FRE-368).
 *
 * Lists all artifacts owned by the current user, newest first. Each card
 * shows title, summary, content-type chip, relative timestamp, and an
 * "Open" link to the public URL.
 *
 * MANUAL TEST PLAN
 * ================
 * Prerequisites: PR #A merged, at least one artifact written via artifact_write.
 *
 * 1. Navigate to /artifacts from the session drawer.
 *    Expected: loading skeleton visible briefly, then a grid of artifact cards
 *    ordered newest-first.
 *
 * 2. Empty state (no artifacts yet).
 *    Expected: "No artifacts yet — ask the agent to make you one." message
 *    centered on the page.
 *
 * 3. Each card shows title (or slug if no title), summary, content-type chip,
 *    and relative timestamp ("5m ago", "2h ago", "3d ago").
 *
 * 4. "Open" link uses target=_blank rel=noopener.
 *    On an installed iOS home-screen PWA: opens in Safari, CF Access SSO
 *    carries over, content renders.
 *
 * 5. Network error during list fetch.
 *    Expected: "Could not load artifacts." error message, no crash.
 *
 * 6. Responsive layout: 1 column on iPhone, 2 on iPad, 3 on laptop.
 */

'use client';

import { useEffect, useState } from 'react';

import { listArtifacts } from '@/lib/agui-client';
import type { ArtifactSummary } from '@/lib/agui-client';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function relativeTime(iso: string): string {
  const diffMs = Date.now() - new Date(iso).getTime();
  const mins = Math.floor(diffMs / 60_000);
  if (mins < 60) return `${Math.max(mins, 1)}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  return `${Math.floor(hrs / 24)}d ago`;
}

function contentTypeLabel(ct: string): string {
  const map: Record<string, string> = {
    'text/html; charset=utf-8': 'HTML',
    'text/markdown; charset=utf-8': 'MD',
    'text/csv; charset=utf-8': 'CSV',
    'application/json': 'JSON',
    'image/png': 'PNG',
    'image/svg+xml': 'SVG',
  };
  return map[ct] ?? ct.split('/')[1]?.toUpperCase() ?? 'FILE';
}

// ---------------------------------------------------------------------------
// Skeleton
// ---------------------------------------------------------------------------

function GridSkeleton() {
  return (
    <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">
      {Array.from({ length: 6 }).map((_, i) => (
        <div
          key={i}
          className="rounded-xl border border-slate-700/60 bg-slate-800/60 p-4 animate-pulse"
        >
          <div className="flex items-center gap-2 mb-2">
            <div className="h-4 w-10 rounded bg-slate-700" />
            <div className="h-4 flex-1 rounded bg-slate-700" />
          </div>
          <div className="h-3 w-4/5 rounded bg-slate-700/70 mb-3" />
          <div className="h-3 w-1/3 rounded bg-slate-700/50" />
        </div>
      ))}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

/** Client-side artifact list for the /artifacts page. */
export function ArtifactsIndex() {
  const [items, setItems] = useState<ArtifactSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    listArtifacts({ type: 'artifact', k: 50 })
      .then(setItems)
      .catch(() => setError('Could not load artifacts.'))
      .finally(() => setLoading(false));
  }, []);

  return (
    <div className="min-h-full bg-slate-900 text-slate-100 px-4 py-6 md:px-8">
      <h1 className="text-lg font-semibold text-slate-100 mb-6">Artifacts</h1>

      {loading && <GridSkeleton />}

      {error && (
        <p className="text-sm text-red-400">{error}</p>
      )}

      {!loading && !error && items.length === 0 && (
        <div className="flex flex-col items-center justify-center py-24 text-slate-500 gap-2">
          <p className="text-sm">No artifacts yet — ask the agent to make you one.</p>
        </div>
      )}

      {!loading && !error && items.length > 0 && (
        <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">
          {items.map((item) => (
            <div
              key={item.artifact_id}
              className="flex flex-col gap-2 rounded-xl border border-slate-700/60 bg-slate-800/60 p-4 hover:border-slate-600 transition-colors"
            >
              {/* Header: chip + title */}
              <div className="flex items-center gap-2">
                <span className="text-xs font-mono px-1.5 py-0.5 rounded bg-slate-700 text-slate-400 flex-shrink-0">
                  {contentTypeLabel(item.content_type)}
                </span>
                <span className="text-sm font-medium text-slate-100 truncate">
                  {item.title ?? item.slug ?? 'Artifact'}
                </span>
              </div>

              {/* Summary */}
              {item.summary && (
                <p className="text-xs text-slate-400 leading-relaxed line-clamp-2">
                  {item.summary}
                </p>
              )}

              {/* Footer: timestamp + open link */}
              <div className="flex items-center justify-between mt-auto pt-1">
                <span className="text-xs text-slate-600">
                  {relativeTime(item.created_at)}
                </span>
                {item.public_url && (
                  <a
                    href={item.public_url}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="text-xs text-slate-400 hover:text-blue-400 transition-colors"
                  >
                    Open ↗
                  </a>
                )}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
