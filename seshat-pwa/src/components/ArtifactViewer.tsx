/**
 * ArtifactViewer — sandboxed iframe overlay for HTML/text artifacts (FRE-368).
 *
 * Implements ADR-0070 D5/D6: single-surface progressive disclosure with a
 * right-side drawer on desktop and a bottom sheet on mobile. The iframe uses
 * sandbox="allow-scripts" per ADR-0089 D2/D3 (supersedes ADR-0070 D7):
 * scripts run inside a unique opaque origin — no PWA session, no storage,
 * no cookies, no parent navigation. The load-bearing boundary is the Worker
 * CSP envelope on artifacts.frenchforet.com (FRE-509); this attribute is
 * defense-in-depth on the embedded path. NEVER add allow-same-origin —
 * it would lift the opaque origin and defeat the isolation model.
 *
 * MANUAL TEST PLAN
 * ================
 * Prerequisites: at least one artifact written via artifact_write with
 * content_type 'text/html; charset=utf-8'.
 *
 * 1. Expand an ArtifactCard inline.
 *    Expected: viewer slides in from the right on laptop (width ≤ max-w-3xl),
 *    slides up from the bottom on iPhone (max-h-[90vh], rounded top corners).
 *
 * 2. Verify sandbox posture (ADR-0089 D2/D3):
 *    Write an artifact containing
 *    <p id="t">static</p><script>document.getElementById('t').textContent='script ran'</script>.
 *    Expand it. Expected: the iframe shows "script ran" (scripts execute),
 *    but the PWA tab title and session are untouched. An artifact calling
 *    localStorage.setItem(...) throws (opaque origin denies storage).
 *
 * 3. Verify iframe cannot navigate parent:
 *    Write an artifact with <script>parent.location='https://evil.com'</script>
 *    or <a href="javascript:parent.location='https://evil.com'">click</a>.
 *    Expected: nothing happens — allow-top-navigation is omitted, so the
 *    sandbox blocks parent navigation even with scripts enabled.
 *
 * 4. ESC key closes the viewer.
 *    Expected: drawer/sheet slides out, chat is fully visible again.
 *
 * 5. Tap the backdrop (dark overlay) on mobile.
 *    Expected: viewer closes.
 *
 * 6. "Open standalone ↗" button.
 *    Expected: artifact opens in a new tab (target=_blank) at
 *    artifacts.frenchforet.com/{id}. On an installed iOS home-screen PWA,
 *    the tab opens in Safari (not in-app WebView), confirming the
 *    WKWebView→Safari handoff fix.
 *
 * 7. Telemetry: DevTools Network tab shows POST /api/v1/telemetry/card_click
 *    with surface='drawer' when opened via the Expand button, and
 *    surface='standalone' when the standalone link is clicked.
 *    Both return 204.
 *
 * 8. Replay cost: open a session with 3 artifact cards expanded then closed.
 *    DevTools Performance → DOM Nodes should stay well under 1 MB (no inline
 *    HTML payload in the transcript, only the iframe URL).
 */

'use client';

import { useEffect, useRef } from 'react';

import { postCardClick } from '@/lib/agui-client';

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
// Props
// ---------------------------------------------------------------------------

interface ArtifactViewerProps {
  artifactId: string;
  publicUrl: string;
  title: string | null;
  contentType: string;
  sessionId?: string;
  onClose: () => void;
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

/**
 * Sandboxed artifact viewer rendered as a right-side drawer (desktop) or
 * bottom sheet (mobile). Closes on ESC or backdrop tap.
 */
export function ArtifactViewer({
  artifactId,
  publicUrl,
  title,
  contentType,
  sessionId,
  onClose,
}: ArtifactViewerProps) {
  const closeRef = useRef<HTMLButtonElement>(null);

  // Focus the close button on mount for keyboard accessibility.
  useEffect(() => {
    closeRef.current?.focus();
  }, []);

  // ESC closes the viewer.
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    document.addEventListener('keydown', handler);
    return () => document.removeEventListener('keydown', handler);
  }, [onClose]);

  const handleStandaloneClick = () => {
    postCardClick(artifactId, 'standalone', sessionId);
  };

  const label = contentTypeLabel(contentType);
  const displayTitle = title ?? 'Artifact';

  return (
    <>
      {/* Backdrop — z-40 so it sits below the panel (z-50) */}
      <div
        className="fixed inset-0 z-40 bg-black/60"
        aria-hidden="true"
        onClick={onClose}
      />

      {/* Panel — right drawer on md+, bottom sheet on mobile */}
      <div
        role="dialog"
        aria-modal="true"
        aria-label={displayTitle}
        className={[
          'fixed z-50 bg-slate-900 border-slate-700 shadow-2xl flex flex-col',
          // Mobile: bottom sheet
          'inset-x-0 bottom-0 max-h-[90vh] rounded-t-2xl border-t',
          // Desktop: right-side drawer
          'md:inset-x-auto md:inset-y-0 md:right-0 md:w-full md:max-w-3xl',
          'md:rounded-none md:border-t-0 md:border-l',
        ].join(' ')}
      >
        {/* Header */}
        <div className="flex items-center gap-2 px-4 py-3 border-b border-slate-700 flex-shrink-0">
          {/* Drag handle — mobile affordance */}
          <div className="md:hidden absolute top-2 left-1/2 -translate-x-1/2 w-10 h-1 rounded-full bg-slate-600" />

          <span
            className="text-xs font-mono px-1.5 py-0.5 rounded bg-slate-700 text-slate-400 flex-shrink-0"
            aria-label={`Content type: ${contentType}`}
          >
            {label}
          </span>

          <h2 className="flex-1 text-sm font-semibold text-slate-100 truncate">
            {displayTitle}
          </h2>

          <a
            href={publicUrl}
            target="_blank"
            rel="noopener noreferrer"
            onClick={handleStandaloneClick}
            className="flex items-center gap-1 text-xs px-1.5 py-0.5 rounded text-slate-400 hover:text-blue-400 hover:bg-slate-700/40 transition-colors flex-shrink-0"
            aria-label="Open standalone in new tab"
          >
            Open ↗
          </a>

          <button
            ref={closeRef}
            onClick={onClose}
            aria-label="Close viewer"
            className="flex items-center justify-center w-6 h-6 rounded text-slate-400 hover:text-slate-100 hover:bg-slate-700/60 transition-colors flex-shrink-0"
          >
            ✕
          </button>
        </div>

        {/* Sandboxed iframe — opaque origin, scripts only (ADR-0089 D2/D3).
            NEVER add allow-same-origin. */}
        <iframe
          src={publicUrl}
          sandbox="allow-scripts"
          referrerPolicy="no-referrer"
          className="flex-1 w-full bg-white"
          title={displayTitle}
        />
      </div>
    </>
  );
}
