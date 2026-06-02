/**
 * ArtifactsIndex — compact list view for the /artifacts route (FRE-368).
 *
 * Tight table-like rows: type chip + title + summary + timestamp + actions.
 * No card grid — one row per artifact, scannable at a glance.
 *
 * MANUAL TEST PLAN
 * ================
 * 1. Navigate to /artifacts from the session drawer.
 *    Expected: compact list of rows, newest first. Each row fits on one line
 *    on iPad landscape; summary truncates with ellipsis on narrow viewports.
 *
 * 2. "← Conversations" back link returns to the most recent session (/).
 *
 * 3. Tap Expand on any row → ArtifactViewer drawer opens.
 *    Tap ESC or backdrop → closes and returns to list.
 *
 * 4. Tap Open ↗ → opens in new tab (Safari on iOS home-screen PWA).
 *
 * 5. Empty state: "No artifacts yet — ask the agent to make you one."
 *
 * 6. Network error: "Could not load artifacts." — no crash.
 */

'use client';

import { useEffect, useState } from 'react';
import Link from 'next/link';

import { listArtifacts } from '@/lib/agui-client';
import type { ArtifactSummary } from '@/lib/agui-client';
import { ArtifactViewer } from './ArtifactViewer';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function relativeTime(iso: string): string {
  const diffMs = Date.now() - new Date(iso).getTime();
  const mins = Math.floor(diffMs / 60_000);
  if (mins < 60) return `${Math.max(mins, 1)}m`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h`;
  return `${Math.floor(hrs / 24)}d`;
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
// Row skeleton
// ---------------------------------------------------------------------------

function ListSkeleton() {
  return (
    <div className="divide-y divide-slate-800">
      {Array.from({ length: 5 }).map((_, i) => (
        <div key={i} className="flex items-center gap-3 px-4 py-3 animate-pulse">
          <div className="h-4 w-10 rounded bg-slate-700 flex-shrink-0" />
          <div className="h-4 w-40 rounded bg-slate-700 flex-shrink-0" />
          <div className="h-3 flex-1 rounded bg-slate-700/60" />
          <div className="h-3 w-8 rounded bg-slate-700/40 flex-shrink-0" />
        </div>
      ))}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

/** Compact list view for the /artifacts route. */
export function ArtifactsIndex() {
  const [items, setItems] = useState<ArtifactSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [viewing, setViewing] = useState<ArtifactSummary | null>(null);

  useEffect(() => {
    listArtifacts({ type: 'artifact', k: 50 })
      .then(setItems)
      .catch(() => setError('Could not load artifacts.'))
      .finally(() => setLoading(false));
  }, []);

  return (
    <div className="min-h-full bg-slate-900 text-slate-100 flex flex-col">
      {/* Viewer overlay */}
      {viewing?.public_url && (
        <ArtifactViewer
          artifactId={viewing.artifact_id}
          publicUrl={viewing.public_url}
          title={viewing.title}
          contentType={viewing.content_type}
          onClose={() => setViewing(null)}
        />
      )}

      {/* Header */}
      <header className="flex items-center gap-3 px-4 border-b border-slate-700 bg-slate-900/80 backdrop-blur-sm flex-shrink-0"
        style={{ paddingTop: 'calc(env(safe-area-inset-top, 0px) + 0.75rem)', paddingBottom: '0.75rem' }}
      >
        <Link
          href="/"
          className="flex items-center gap-1 text-sm text-slate-400 hover:text-slate-100 transition-colors"
        >
          ← Conversations
        </Link>
        <span className="text-slate-700">|</span>
        <h1 className="text-sm font-semibold text-slate-100">Artifacts</h1>
      </header>

      {/* List */}
      <main className="flex-1 overflow-y-auto">
        {loading && <ListSkeleton />}

        {error && (
          <p className="px-4 py-6 text-sm text-red-400">{error}</p>
        )}

        {!loading && !error && items.length === 0 && (
          <div className="flex flex-col items-center justify-center py-24 text-slate-500">
            <p className="text-sm">No artifacts yet — ask the agent to make you one.</p>
          </div>
        )}

        {!loading && !error && items.length > 0 && (
          <div className="divide-y divide-slate-800/60">
            {items.map((item) => (
              <div
                key={item.artifact_id}
                className="flex items-center gap-3 px-4 py-2.5 hover:bg-slate-800/40 transition-colors"
              >
                {/* Type chip */}
                <span className="text-xs font-mono font-semibold tracking-wide px-1.5 py-0.5 rounded bg-slate-600 text-slate-100 flex-shrink-0 w-10 text-center">
                  {contentTypeLabel(item.content_type)}
                </span>

                {/* Title — fixed width, truncated */}
                <span className="text-sm font-medium text-slate-100 truncate w-40 flex-shrink-0">
                  {item.title ?? item.slug ?? 'Artifact'}
                </span>

                {/* Summary — fills remaining space */}
                <span className="text-xs text-slate-500 truncate flex-1 hidden sm:block">
                  {item.summary ?? ''}
                </span>

                {/* Timestamp */}
                <span className="text-xs text-slate-600 flex-shrink-0 w-8 text-right">
                  {relativeTime(item.created_at)}
                </span>

                {/* Actions */}
                {item.public_url && (
                  <div className="flex items-center gap-2 flex-shrink-0">
                    <button
                      onClick={() => setViewing(item)}
                      className="text-xs text-slate-400 hover:text-blue-400 transition-colors"
                    >
                      Expand
                    </button>
                    <a
                      href={item.public_url}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="text-xs text-slate-400 hover:text-blue-400 transition-colors"
                    >
                      Open ↗
                    </a>
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
      </main>
    </div>
  );
}
