/**
 * ArtifactCard — inline card rendered in chat when an assistant message
 * contains an artifacts.example.com/{uuid} URL (FRE-368, ADR-0070 Tier 3).
 *
 * Fetches metadata from GET /api/v1/artifacts/{id} on mount. On success
 * renders a compact card (title, summary, content-type chip, Expand button,
 * Open standalone link). On failure falls back to a plain external link so
 * a broken artifact never breaks the chat transcript.
 *
 * MANUAL TEST PLAN
 * ================
 * Prerequisites: backend running with PR #A merged; at least one artifact
 * written via artifact_write.
 *
 * 1. Ask the agent to write an HTML artifact. The assistant reply should
 *    contain the public_url (artifacts.example.com/{id}).
 *    Expected: the URL renders as a card with the artifact's title and a
 *    one-line summary — NOT as a bare hyperlink.
 *
 * 2. Card loading state.
 *    Expected: skeleton pulse animation visible briefly before metadata loads.
 *
 * 3. Card error/404 fallback.
 *    Type a message containing a made-up artifacts URL (wrong UUID).
 *    Expected: card degrades to a plain blue underlined hyperlink — no crash,
 *    no empty box.
 *
 * 4. "Expand" opens the ArtifactViewer.
 *    Expected: drawer/sheet slides in with the sandboxed iframe.
 *    Viewer closes on ESC or backdrop tap.
 *    DevTools Network shows POST /api/v1/telemetry/card_click → 204.
 *
 * 5. "Open standalone ↗" opens in a new tab.
 *    Expected: target=_blank opens artifacts.example.com/{id} in Safari
 *    (not in-app WebView) when triggered from an installed iOS home-screen
 *    PWA. CF Access SSO covers the tab — no re-auth prompt.
 *
 * 6. Multiple artifact cards in one message all load independently.
 *    Expected: each card fetches its own metadata; one 404 doesn't affect
 *    the others.
 *
 * 7. Unmount before fetch completes (navigate away quickly after send).
 *    Expected: no "Can't perform state update on unmounted component" warning
 *    in the console (AbortController cancels the in-flight request).
 */

'use client';

import { useEffect, useState } from 'react';

import { getArtifactMetadata, postCardClick } from '@/lib/agui-client';
import type { ArtifactSummary } from '@/lib/agui-client';
import { ArtifactViewer } from './ArtifactViewer';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

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
// Loading skeleton
// ---------------------------------------------------------------------------

function CardSkeleton() {
  return (
    <div className="inline-flex flex-col gap-1.5 rounded-xl border border-slate-700/60 bg-slate-800/60 px-3 py-2.5 my-1 w-full max-w-sm animate-pulse">
      <div className="flex items-center gap-2">
        <div className="h-4 w-10 rounded bg-slate-700" />
        <div className="h-4 flex-1 rounded bg-slate-700" />
      </div>
      <div className="h-3 w-4/5 rounded bg-slate-700/70" />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

interface ArtifactCardProps {
  artifactId: string;
  fallbackHref: string;
  sessionId?: string;
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

/**
 * Inline artifact card for Tier 3 chat content (ADR-0070 D5).
 *
 * Rendered by MarkdownContent's link handler when an href matches the
 * artifacts host. Fetches metadata asynchronously; falls back to a plain
 * link on any error.
 */
export function ArtifactCard({ artifactId, fallbackHref, sessionId }: ArtifactCardProps) {
  const [meta, setMeta] = useState<ArtifactSummary | null>(null);
  const [loading, setLoading] = useState(true);
  const [viewerOpen, setViewerOpen] = useState(false);

  useEffect(() => {
    const ctrl = new AbortController();

    getArtifactMetadata(artifactId)
      .then((data) => {
        if (!ctrl.signal.aborted) {
          setMeta(data);
        }
      })
      .catch(() => {
        // 404 or network error — fall back to plain link (meta stays null)
      })
      .finally(() => {
        if (!ctrl.signal.aborted) setLoading(false);
      });

    return () => ctrl.abort();
  }, [artifactId]);

  if (loading) return <CardSkeleton />;

  // Fallback — artifact not found or fetch failed
  if (!meta || !meta.public_url) {
    return (
      <a
        href={fallbackHref}
        target="_blank"
        rel="noopener noreferrer"
        className="text-blue-400 underline underline-offset-2 hover:text-blue-300"
      >
        {fallbackHref}
      </a>
    );
  }

  const label = contentTypeLabel(meta.content_type);

  const handleExpand = () => {
    postCardClick(artifactId, 'inline', sessionId);
    setViewerOpen(true);
  };

  return (
    <>
      {/* Inline card */}
      <div className="inline-flex flex-col gap-1 rounded-xl border border-slate-700/60 bg-slate-800/60 px-3 py-2.5 my-1 w-full max-w-sm">
        {/* Header row: type chip + title */}
        <div className="flex items-center gap-2">
          <span
            className="text-xs font-mono font-semibold tracking-wide px-1.5 py-0.5 rounded bg-slate-600 text-slate-100 flex-shrink-0"
            aria-label={`Content type: ${meta.content_type}`}
          >
            {label}
          </span>
          <span className="text-sm font-medium text-slate-100 truncate">
            {meta.title ?? meta.slug ?? 'Artifact'}
          </span>
        </div>

        {/* Summary */}
        {meta.summary && (
          <p className="text-xs text-slate-400 leading-relaxed line-clamp-2">
            {meta.summary}
          </p>
        )}

        {/* Actions */}
        <div className="flex items-center gap-2 mt-0.5">
          <button
            onClick={handleExpand}
            className="flex items-center gap-1 text-xs px-1.5 py-0.5 rounded hover:text-blue-400 hover:bg-slate-700/40 text-slate-400 transition-colors"
          >
            Expand
          </button>
          <a
            href={meta.public_url}
            target="_blank"
            rel="noopener noreferrer"
            onClick={() => postCardClick(artifactId, 'standalone', sessionId)}
            className="flex items-center gap-1 text-xs px-1.5 py-0.5 rounded hover:text-blue-400 hover:bg-slate-700/40 text-slate-400 transition-colors"
          >
            Open ↗
          </a>
        </div>
      </div>

      {/* Viewer overlay — rendered in place so it sits above the chat */}
      {viewerOpen && (
        <ArtifactViewer
          artifactId={artifactId}
          publicUrl={meta.public_url}
          title={meta.title}
          contentType={meta.content_type}
          sessionId={sessionId}
          onClose={() => setViewerOpen(false)}
        />
      )}
    </>
  );
}
