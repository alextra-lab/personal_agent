/**
 * ArtifactExportMenu — "Export ▾" control for the artifact drawer (FRE-549).
 *
 * Wires the live FRE-530 endpoint
 * (`GET /api/v1/artifacts/{id}/export?mode=inline|substitute`, ADR-0089 A5)
 * to a small dropdown on the artifact viewer header. Only mounted for HTML
 * artifacts by the parent (the endpoint 400s otherwise).
 *
 * Two modes:
 *   - Offline (portable) → `mode=inline`     — self-contained, renders anywhere.
 *   - Online (lean)      → `mode=substitute` — CDN refs + SRI, needs internet.
 *
 * Download mechanism is fetch → blob → object URL → anchor[download] → click →
 * revoke (not a plain `<a href>`) so the failure codes are observable: inline
 * export `502`s until the CF service token is authorized, which we surface as a
 * friendly "try Online" message rather than a broken download.
 */

'use client';

import { useEffect, useRef, useState } from 'react';

import {
  ArtifactExportError,
  fetchArtifactExport,
  type ArtifactExportMode,
} from '@/lib/agui-client';

interface ArtifactExportMenuProps {
  artifactId: string;
  /** Client-suggested download filename (server's Content-Disposition wins). */
  filename: string;
}

/** Stream a fetched Blob to the user as a file download, then release the URL. */
function downloadBlob(blob: Blob, filename: string): void {
  if (typeof document === 'undefined') return;
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
}

/**
 * Export dropdown for a hosted HTML artifact. Closes on outside click; shows an
 * inline, non-fatal error message when the export endpoint fails.
 */
export function ArtifactExportMenu({ artifactId, filename }: ArtifactExportMenuProps) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const rootRef = useRef<HTMLDivElement>(null);

  // Close the menu on an outside click.
  useEffect(() => {
    if (!open) return;
    const handler = (e: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    };
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, [open]);

  const handleExport = async (mode: ArtifactExportMode) => {
    setBusy(true);
    setError(null);
    try {
      const blob = await fetchArtifactExport(artifactId, mode);
      downloadBlob(blob, filename);
      setOpen(false);
    } catch (e) {
      if (e instanceof ArtifactExportError && e.status === 502) {
        setError('Offline export unavailable — try Online (lean).');
      } else {
        const status = e instanceof ArtifactExportError ? ` (${e.status})` : '';
        setError(`Export failed${status}.`);
      }
    } finally {
      setBusy(false);
    }
  };

  return (
    <div ref={rootRef} className="relative flex-shrink-0">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        disabled={busy}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label="Export artifact"
        className="flex items-center gap-1 text-xs px-1.5 py-0.5 rounded text-slate-400 hover:text-blue-400 hover:bg-slate-700/40 transition-colors disabled:opacity-60"
      >
        {busy ? 'Exporting…' : 'Export ▾'}
      </button>

      {open && (
        <div
          role="menu"
          className="absolute right-0 top-full mt-1 z-50 w-56 rounded-md border border-slate-700 bg-slate-800 shadow-xl py-1"
        >
          <button
            type="button"
            role="menuitem"
            onClick={() => handleExport('inline')}
            disabled={busy}
            className="w-full text-left px-3 py-2 hover:bg-slate-700/60 transition-colors disabled:opacity-60"
          >
            <span className="block text-xs font-medium text-slate-100">Offline (portable)</span>
            <span className="block text-[11px] text-slate-400">Self-contained, opens anywhere</span>
          </button>
          <button
            type="button"
            role="menuitem"
            onClick={() => handleExport('substitute')}
            disabled={busy}
            className="w-full text-left px-3 py-2 hover:bg-slate-700/60 transition-colors disabled:opacity-60"
          >
            <span className="block text-xs font-medium text-slate-100">Online (lean)</span>
            <span className="block text-[11px] text-slate-400">CDN refs + SRI, needs internet</span>
          </button>
        </div>
      )}

      {error && (
        <p
          role="alert"
          className="absolute right-0 top-full mt-1 z-50 w-56 rounded-md border border-amber-700/60 bg-amber-950/80 px-3 py-2 text-[11px] text-amber-200"
        >
          {error}
        </p>
      )}
    </div>
  );
}
