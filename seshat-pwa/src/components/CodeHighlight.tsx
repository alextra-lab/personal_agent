'use client';

import { useMemo } from 'react';
import hljs from 'highlight.js/lib/common';

interface CodeHighlightProps {
  /** Fenced-block language hint (e.g. "python"); "" or unknown → auto-detect. */
  language: string;
  /** Raw source text of the code block. */
  code: string;
}

function escapeHtml(text: string): string {
  return text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
}

/**
 * Syntax-highlights a code block with highlight.js, aligned to the curated
 * artifact toolkit (highlight.js@11.9.0, github-dark theme; FRE-532).
 *
 * Uses the `lib/common` build — the same ~37-language subset the toolkit's
 * `@highlightjs/cdn-assets` "common" bundle is generated from. A registered
 * language is highlighted directly; anything else falls back to auto-detection,
 * and any highlight error degrades to escaped plain text. highlight.js escapes
 * the source, so HTML inside a fence (e.g. `<script>`) renders as inert text,
 * never a live node — the value is therefore safe for `dangerouslySetInnerHTML`.
 *
 * Args:
 *     language: Fenced-block language hint; empty or unknown triggers auto-detect.
 *     code: Raw source text of the code block.
 */
export function CodeHighlight({ language, code }: CodeHighlightProps) {
  const html = useMemo(() => {
    try {
      if (language && hljs.getLanguage(language)) {
        return hljs.highlight(code, { language, ignoreIllegals: true }).value;
      }
      return hljs.highlightAuto(code).value;
    } catch {
      // hljs throws on an unregistered language — never let it bubble.
      return escapeHtml(code);
    }
  }, [language, code]);

  return (
    <code
      className={`hljs language-${language || 'text'}`}
      dangerouslySetInnerHTML={{ __html: html }}
    />
  );
}
