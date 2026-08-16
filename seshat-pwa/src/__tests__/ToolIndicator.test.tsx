/**
 * FRE-1264 AC-6 — ToolIndicator (the live per-tool list shown while a turn
 * is streaming) collapses to a single summary line once more than one tool
 * is active, expandable to today's per-tool detail. A single active tool
 * keeps rendering directly, unchanged.
 */

import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, it, expect } from 'vitest';

import { ToolIndicator } from '@/components/ToolIndicator';
import type { ToolCall } from '@/lib/types';

function tool(overrides: Partial<ToolCall>): ToolCall {
  return { name: 'read_file', status: 'completed', ...overrides };
}

describe('ToolIndicator', () => {
  it('renders nothing for zero tools', () => {
    const { container } = render(<ToolIndicator tools={[]} />);
    expect(container).toBeEmptyDOMElement();
  });

  it('renders a single tool directly, not behind a summary toggle', () => {
    render(<ToolIndicator tools={[tool({ name: 'run_python' })]} />);
    expect(screen.getByText('run_python')).toBeInTheDocument();
    expect(screen.queryByRole('button')).toBeNull();
  });

  it('collapses more than one tool behind a summary button by default', () => {
    render(
      <ToolIndicator
        tools={[tool({ name: 'read_file' }), tool({ name: 'run_python' })]}
      />,
    );
    // Per-tool detail is not in the DOM until expanded.
    expect(screen.queryByText('read_file')).toBeNull();
    expect(screen.queryByText('run_python')).toBeNull();

    const toggle = screen.getByRole('button');
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    expect(toggle.textContent).toContain('2');
  });

  it('shows a present-tense summary while any tool is still running', () => {
    render(
      <ToolIndicator
        tools={[tool({ name: 'read_file' }), tool({ name: 'run_python', status: 'running' })]}
      />,
    );
    expect(screen.getByRole('button').textContent?.toLowerCase()).toContain('using');
  });

  it('shows a past-tense summary once every tool is completed', () => {
    render(
      <ToolIndicator
        tools={[tool({ name: 'read_file' }), tool({ name: 'run_python' })]}
      />,
    );
    expect(screen.getByRole('button').textContent?.toLowerCase()).toContain('used');
  });

  it('expands to reveal per-tool detail on activation', async () => {
    const user = userEvent.setup();
    render(
      <ToolIndicator
        tools={[tool({ name: 'read_file' }), tool({ name: 'run_python', status: 'running' })]}
      />,
    );

    const toggle = screen.getByRole('button');
    await user.click(toggle);

    expect(toggle).toHaveAttribute('aria-expanded', 'true');
    expect(screen.getByText('read_file')).toBeInTheDocument();
    expect(screen.getByText('run_python')).toBeInTheDocument();
  });
});
