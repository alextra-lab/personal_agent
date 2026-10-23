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
  // FRE-1651: single dollar signs are money, not math. Inline math is written
  // with double dollar signs inside a paragraph.
  it('renders inline math written with double dollar signs', () => {
    const { container } = render(<MarkdownContent content={'Energy is $$E=mc^2$$.'} />);
    expect(container.querySelector('.katex')).not.toBeNull();
    expect(container.querySelector('.katex-display')).toBeNull();
  });

  // FRE-1651 AC-1 / AC-2: this test fails against `remarkMath` with its defaults.
  it('renders dollar amounts as plain text with their dollar signs', () => {
    const content =
      'Fares run $554–816 typical; cheapest $506. Other dates: $425–580 against $660–820.';
    const { container } = render(<MarkdownContent content={content} />);
    expect(container.querySelector('.katex')).toBeNull();
    for (const amount of ['$554–816', '$506', '$425–580', '$660–820']) {
      expect(container.textContent).toContain(amount);
    }
  });

  // FRE-1651 AC-3
  it('renders a block with double dollar signs as display math', () => {
    const { container } = render(
      <MarkdownContent content={'$$\nx^2 + y^2\n$$'} />,
    );
    expect(container.querySelector('.katex-display')).not.toBeNull();
  });

  // FRE-1651: a wide table and a wide display-math block each scroll inside their own box.
  it('wraps a table in its own horizontal scroll box', () => {
    const { container } = render(
      <MarkdownContent content={'| a | b |\n|---|---|\n| 1 | 2 |'} />,
    );
    const table = container.querySelector('table');
    expect(table).not.toBeNull();
    expect(table!.parentElement!.className).toContain('overflow-x-auto');
  });

  it('gives display math its own horizontal scroll box', () => {
    const { container } = render(
      <MarkdownContent content={'$$\nx^2 + y^2\n$$'} />,
    );
    const display = container.querySelector('.katex-display');
    expect(display).not.toBeNull();
    expect(display!.parentElement!.className).toContain('overflow-x-auto');
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
