/**
 * Tests for MarkdownContent toolkit convergence (FRE-532, ADR-0089 Addendum A6).
 *
 * The PWA chat must render math (KaTeX), code (highlight.js), and diagrams
 * (client mermaid) with libs/versions aligned to the curated artifact toolkit,
 * never loading executable scripts cross-origin from the artifact host.
 */

import { render } from '@testing-library/react';
import { vi, describe, it, expect } from 'vitest';

// Mermaid is dynamically imported by MermaidBlock; stub it so the routing test
// is deterministic and free of ESM-load noise in jsdom.
vi.mock('mermaid', () => ({
  default: {
    initialize: vi.fn(),
    render: vi.fn().mockResolvedValue({ svg: '<svg></svg>' }),
  },
}));

import { MarkdownContent } from '@/components/MarkdownContent';

describe('MarkdownContent — math (KaTeX)', () => {
  it('renders inline math', () => {
    const { container } = render(<MarkdownContent content={'Energy is $E=mc^2$.'} />);
    expect(container.querySelector('.katex')).not.toBeNull();
  });

  it('renders display math', () => {
    const { container } = render(
      <MarkdownContent content={'$$\n\\int_0^1 x\\,dx\n$$'} />,
    );
    expect(container.querySelector('.katex-display')).not.toBeNull();
  });
});

describe('MarkdownContent — code (highlight.js)', () => {
  it('highlights a fenced python block with hljs', () => {
    const { container } = render(
      <MarkdownContent content={'```python\ndef f():\n    pass\n```'} />,
    );
    const code = container.querySelector('code.hljs');
    expect(code).not.toBeNull();
    expect(container.querySelector('.hljs-keyword')).not.toBeNull();
  });

  it('does not throw on an unknown language', () => {
    expect(() =>
      render(<MarkdownContent content={'```fakelang\nx = 1\n```'} />),
    ).not.toThrow();
  });

  it('escapes HTML inside a code fence (no live script node)', () => {
    const { container } = render(
      <MarkdownContent content={'```html\n<script>alert(1)</script>\n```'} />,
    );
    // The code text is rendered as inert, escaped text — never a live element.
    expect(container.querySelector('code script')).toBeNull();
    expect(container.textContent).toContain('alert(1)');
  });
});

describe('MarkdownContent — diagrams (client mermaid)', () => {
  it('routes a mermaid fence to MermaidBlock', () => {
    const { getByText } = render(
      <MarkdownContent content={'```mermaid\ngraph LR;A-->B\n```'} />,
    );
    // MermaidBlock always renders a "figure" label in its header.
    expect(getByText('figure')).toBeInTheDocument();
  });
});
