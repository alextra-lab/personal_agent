'use client';

import { useEffect, useId, useRef, useState } from 'react';
import { Prism as SyntaxHighlighter } from 'react-syntax-highlighter';
import { oneDark } from 'react-syntax-highlighter/dist/esm/styles/prism';

interface MermaidBlockProps {
  chart: string;
}

type Status = 'loading' | 'rendered' | 'error';
type View = 'diagram' | 'source';

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

/**
 * Renders a mermaid diagram fenced block as an inline SVG figure.
 *
 * Lazy-loads mermaid.js via dynamic import (code-split by Next.js).
 * SVG is inserted via DOMParser + replaceChildren — no string-injection patterns.
 * Falls back to syntax-highlighted source on parse error.
 * Includes a figure/source toggle matching the existing CodeBlock chrome.
 */
export function MermaidBlock({ chart }: MermaidBlockProps) {
  const uid = useId();
  const renderId = useRef(`mmd-${uid.replace(/:/g, '')}`);
  const containerRef = useRef<HTMLDivElement>(null);
  const [status, setStatus] = useState<Status>('loading');
  const [errorMsg, setErrorMsg] = useState('');
  const [view, setView] = useState<View>('diagram');

  useEffect(() => {
    let cancelled = false;

    if (containerRef.current) {
      containerRef.current.replaceChildren();
    }

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

        // Parse via DOMParser using text/html — mermaid's browser-side render()
        // emits SVG fragments that assume inline-HTML context (e.g. C4 diagrams
        // use `xlink:href` without declaring `xmlns:xlink` on the root <svg>).
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

  const isSource = view === 'source';

  return (
    <div className="relative my-3 rounded-lg overflow-hidden border border-slate-800/70 ring-1 ring-inset ring-slate-700/30">
      {/* Header — lighter chrome than CodeBlock so the diagram gets the visual weight */}
      <div className="flex items-center justify-between px-3 py-1.5 bg-slate-800/80 border-b border-slate-800/60">
        <div className="flex items-center gap-1.5 text-slate-500">
          <DiamondIcon />
          <span className="text-xs font-mono">figure</span>
        </div>
        {status !== 'loading' && (
          <button
            onClick={() => setView(isSource ? 'diagram' : 'source')}
            disabled={status === 'error'}
            className="text-xs text-slate-500 hover:text-seshat-accent transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
          >
            {isSource ? 'view diagram' : 'view source'}
          </button>
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
          <SyntaxHighlighter
            style={oneDark}
            language="text"
            PreTag="div"
            customStyle={{
              margin: 0,
              borderRadius: 0,
              fontSize: '0.75rem',
              background: '#0d1117',
            }}
          >
            {chart}
          </SyntaxHighlighter>
        </div>
      )}
    </div>
  );
}
