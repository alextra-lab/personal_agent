'use client';

import { useEffect, useId, useRef, useState } from 'react';
import { CodeHighlight } from './CodeHighlight';

interface MermaidBlockProps {
  chart: string;
}

type Status = 'loading' | 'rendered' | 'error';
type View = 'diagram' | 'source';
type FeedbackKind = 'svg' | 'png' | 'copy';

const PNG_DPR = 2;
const PNG_BACKGROUND = '#0f172a';
const FEEDBACK_MS = 1400;

function DiamondIcon() {
  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      viewBox="0 0 12 12"
      fill="none"
      stroke="currentColor"
      strokeWidth="1"
      className="w-3 h-3 opacity-60"
      aria-hidden
    >
      <polygon points="6,1 11,6 6,11 1,6" />
      <polygon points="6,3 9,6 6,9 3,6" />
    </svg>
  );
}

function DownloadIcon() {
  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.4"
      strokeLinecap="round"
      strokeLinejoin="round"
      className="w-3.5 h-3.5"
      aria-hidden
    >
      <path d="M8 2v8" />
      <path d="M4.5 6.5L8 10l3.5-3.5" />
      <path d="M2.5 13h11" />
    </svg>
  );
}

function CopyIcon() {
  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      viewBox="0 0 16 16"
      fill="currentColor"
      className="w-3.5 h-3.5"
      aria-hidden
    >
      <path d="M5.5 3.5A1.5 1.5 0 0 1 7 2h2.879a1.5 1.5 0 0 1 1.06.44l2.122 2.12a1.5 1.5 0 0 1 .439 1.061V9.5A1.5 1.5 0 0 1 12 11H9.5a.5.5 0 0 1 0-1H12a.5.5 0 0 0 .5-.5V6H10a1 1 0 0 1-1-1V2.5H7a.5.5 0 0 0-.5.5v1a.5.5 0 0 1-1 0V3.5Z" />
      <path d="M4.5 6a1.5 1.5 0 0 0-1.5 1.5v5A1.5 1.5 0 0 0 4.5 14h3A1.5 1.5 0 0 0 9 12.5v-5A1.5 1.5 0 0 0 7.5 6h-3Zm0 1h3a.5.5 0 0 1 .5.5v5a.5.5 0 0 1-.5.5h-3a.5.5 0 0 1-.5-.5v-5a.5.5 0 0 1 .5-.5Z" />
    </svg>
  );
}

function CheckIcon() {
  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.6"
      strokeLinecap="round"
      strokeLinejoin="round"
      className="w-3.5 h-3.5"
      aria-hidden
    >
      <path d="M3 8.5L6.5 12 13 5" />
    </svg>
  );
}

/** Parse the SVG viewBox to get intrinsic pixel dimensions for canvas export. */
function parseSvgDimensions(svgMarkup: string): { width: number; height: number } {
  const match = svgMarkup.match(/viewBox="([-\d.\s]+)"/);
  if (match) {
    const parts = match[1].trim().split(/\s+/).map(Number);
    if (parts.length === 4 && parts.every((n) => Number.isFinite(n))) {
      return { width: Math.max(1, parts[2]), height: Math.max(1, parts[3]) };
    }
  }
  return { width: 800, height: 600 };
}

function triggerDownload(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  a.rel = 'noopener';
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

/** Rasterize an SVG markup string to a PNG blob at the configured DPR. */
async function rasterizeToPng(svgMarkup: string): Promise<Blob> {
  const { width, height } = parseSvgDimensions(svgMarkup);
  const svgBlob = new Blob([svgMarkup], { type: 'image/svg+xml;charset=utf-8' });
  const svgUrl = URL.createObjectURL(svgBlob);
  try {
    const img = new Image();
    await new Promise<void>((resolve, reject) => {
      img.onload = () => resolve();
      img.onerror = () => reject(new Error('Could not load SVG as image'));
      img.src = svgUrl;
    });
    const canvas = document.createElement('canvas');
    canvas.width = width * PNG_DPR;
    canvas.height = height * PNG_DPR;
    const ctx = canvas.getContext('2d');
    if (!ctx) throw new Error('Canvas context unavailable');
    ctx.fillStyle = PNG_BACKGROUND;
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
    const blob = await new Promise<Blob | null>((resolve) =>
      canvas.toBlob(resolve, 'image/png'),
    );
    if (!blob) throw new Error('Canvas could not encode PNG');
    return blob;
  } finally {
    URL.revokeObjectURL(svgUrl);
  }
}

/**
 * Renders a mermaid diagram fenced block as an inline SVG figure with
 * download (SVG / PNG) and copy-source actions in the header.
 *
 * Lazy-loads mermaid.js via dynamic import (code-split by Next.js).
 * SVG is parsed via text/html (handles xlink namespace correctly for C4)
 * and inserted via replaceChildren — no string-injection patterns.
 * Falls back to syntax-highlighted source on parse error.
 */
export function MermaidBlock({ chart }: MermaidBlockProps) {
  const uid = useId();
  const renderId = useRef(`mmd-${uid.replace(/:/g, '')}`);
  const containerRef = useRef<HTMLDivElement>(null);
  const svgMarkupRef = useRef<string>('');
  const [status, setStatus] = useState<Status>('loading');
  const [errorMsg, setErrorMsg] = useState('');
  const [view, setView] = useState<View>('diagram');
  const [feedback, setFeedback] = useState<FeedbackKind | null>(null);

  useEffect(() => {
    let cancelled = false;

    if (containerRef.current) {
      containerRef.current.replaceChildren();
    }
    svgMarkupRef.current = '';

    (async () => {
      try {
        const mermaid = (await import('mermaid')).default;
        mermaid.initialize({
          startOnLoad: false,
          securityLevel: 'strict',
          theme: 'base',
          themeVariables: {
            primaryColor: '#1e293b',
            primaryTextColor: '#e2e8f0',
            primaryBorderColor: '#3b82f6',
            lineColor: '#475569',
            secondaryColor: '#334155',
            tertiaryColor: '#0f172a',
            background: '#0f172a',
            mainBkg: '#1e293b',
            nodeBorder: '#3b82f6',
            clusterBkg: '#0f172a',
            titleColor: '#e2e8f0',
            edgeLabelBackground: '#1e293b',
            fontFamily: 'inherit',
          },
        });

        const { svg } = await mermaid.render(renderId.current, chart);
        if (cancelled) return;

        svgMarkupRef.current = svg;

        // Parse via DOMParser using text/html — mermaid's browser-side render()
        // emits SVG fragments that assume inline-HTML context (e.g. C4 diagrams
        // use xlink:href without declaring xmlns:xlink on the root <svg>).
        // image/svg+xml is strict XML and rejects undeclared namespaces; the
        // HTML5 parser handles SVG-in-HTML correctly per the WHATWG spec and
        // preserves SVG attribute case. mermaid's securityLevel:'strict' already
        // sanitized scripts and handlers from the content.
        if (containerRef.current) {
          const parser = new DOMParser();
          const doc = parser.parseFromString(
            `<!doctype html><body>${svg}`,
            'text/html',
          );
          const svgEl = doc.body.firstElementChild;
          if (svgEl) {
            containerRef.current.replaceChildren(svgEl);
          }
        }
        setStatus('rendered');
      } catch (err) {
        if (cancelled) return;
        const msg = err instanceof Error ? err.message : String(err);
        setErrorMsg(msg.slice(0, 120));
        setView('source');
        setStatus('error');
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [chart]);

  const flashFeedback = (kind: FeedbackKind) => {
    setFeedback(kind);
    window.setTimeout(() => {
      setFeedback((current) => (current === kind ? null : current));
    }, FEEDBACK_MS);
  };

  const handleDownloadSvg = () => {
    if (!svgMarkupRef.current) return;
    const blob = new Blob([svgMarkupRef.current], {
      type: 'image/svg+xml;charset=utf-8',
    });
    triggerDownload(blob, 'diagram.svg');
    flashFeedback('svg');
  };

  const handleDownloadPng = async () => {
    if (!svgMarkupRef.current) return;
    try {
      const blob = await rasterizeToPng(svgMarkupRef.current);
      triggerDownload(blob, 'diagram.png');
      flashFeedback('png');
    } catch {
      // Rasterization failed (rare — usually iOS PWA quirk with cross-origin
      // SVG fonts). Silent — user still has SVG download as the fallback.
    }
  };

  const handleCopySource = async () => {
    if (!svgMarkupRef.current || !navigator.clipboard) return;
    try {
      await navigator.clipboard.writeText(svgMarkupRef.current);
      flashFeedback('copy');
    } catch {
      // Clipboard write failed (insecure context or permission denied) — silent.
    }
  };

  const isSource = view === 'source';
  const canAct = status === 'rendered';

  return (
    <div className="relative my-3 rounded-lg overflow-hidden border border-slate-800/70 ring-1 ring-inset ring-slate-700/30">
      {/* Header — lighter chrome than CodeBlock so the diagram gets the visual weight */}
      <div className="flex items-center justify-between gap-2 px-3 py-1.5 bg-slate-800/80 border-b border-slate-800/60">
        <div className="flex items-center gap-1.5 text-slate-500 shrink-0">
          <DiamondIcon />
          <span className="text-xs font-mono">figure</span>
        </div>
        {canAct && (
          <div className="flex items-center gap-1 text-slate-500">
            <button
              type="button"
              onClick={handleDownloadSvg}
              title="Download SVG"
              aria-label="Download as SVG"
              className="flex items-center gap-1 text-xs px-1.5 py-0.5 rounded hover:text-seshat-accent hover:bg-slate-700/40 transition-colors"
            >
              {feedback === 'svg' ? <CheckIcon /> : <DownloadIcon />}
              <span className="hidden sm:inline">svg</span>
            </button>
            <button
              type="button"
              onClick={handleDownloadPng}
              title="Download PNG"
              aria-label="Download as PNG"
              className="flex items-center gap-1 text-xs px-1.5 py-0.5 rounded hover:text-seshat-accent hover:bg-slate-700/40 transition-colors"
            >
              {feedback === 'png' ? <CheckIcon /> : <DownloadIcon />}
              <span className="hidden sm:inline">png</span>
            </button>
            <button
              type="button"
              onClick={handleCopySource}
              title="Copy SVG markup"
              aria-label="Copy SVG markup"
              className="flex items-center gap-1 text-xs px-1.5 py-0.5 rounded hover:text-seshat-accent hover:bg-slate-700/40 transition-colors"
            >
              {feedback === 'copy' ? <CheckIcon /> : <CopyIcon />}
              <span className="hidden sm:inline">copy</span>
            </button>
            <span className="mx-1 h-3 w-px bg-slate-700/60" aria-hidden />
            <button
              type="button"
              onClick={() => setView(isSource ? 'diagram' : 'source')}
              className="text-xs px-1.5 py-0.5 rounded hover:text-seshat-accent hover:bg-slate-700/40 transition-colors"
            >
              {isSource ? 'view diagram' : 'view source'}
            </button>
          </div>
        )}
        {status === 'error' && (
          <span className="text-xs text-slate-600">source</span>
        )}
      </div>

      {/* Loading state — three pulsing dots, reuses the tailwind.config pulse keyframe */}
      {status === 'loading' && (
        <div className="flex items-center justify-center gap-1.5 min-h-[120px] bg-slate-900/40">
          {[0, 200, 400].map((delay) => (
            <span
              key={delay}
              className="w-1.5 h-1.5 rounded-full bg-slate-600 animate-pulse-dot"
              style={{ animationDelay: `${delay}ms` }}
            />
          ))}
        </div>
      )}

      {/* SVG container — always mounted so the ref is available to the effect */}
      <div
        ref={containerRef}
        className={[
          'flex items-center justify-center p-6 sm:p-8',
          'bg-gradient-to-b from-slate-900/60 to-slate-900/20',
          'overflow-x-auto',
          status !== 'rendered' || isSource ? 'hidden' : '',
        ].join(' ')}
      />

      {/* Source / error fallback */}
      {status !== 'loading' && isSource && (
        <div>
          {status === 'error' && (
            <div className="px-3 pt-2 border-l-2 border-rose-400/60">
              <p className="text-xs text-rose-300/80">{errorMsg}</p>
            </div>
          )}
          <pre className="m-0 p-3 overflow-x-auto text-xs leading-relaxed bg-[#0d1117]">
            <CodeHighlight language="text" code={chart} />
          </pre>
        </div>
      )}
    </div>
  );
}
