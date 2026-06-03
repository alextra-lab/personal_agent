/**
 * Tests for DecisionCard component (ADR-0076 / FRE-400 WS2).
 */

import { render, screen, fireEvent } from '@testing-library/react';
import { vi, describe, it, expect, beforeEach } from 'vitest';

vi.mock('@/lib/agui-client', () => ({
  SESHAT_API: 'http://localhost:9000',
  authHeaders: () => ({}),
}));

import { DecisionCard } from '@/components/DecisionCard';
import type { PendingConstraint } from '@/lib/types';

const TOOL_LIMIT_CONSTRAINT: PendingConstraint = {
  request_id: 'req-001',
  constraint: 'tool_iteration_limit',
  context: 'You have used 10 tool iterations.',
  options: ['continue_10', 'finish_now'],
  default_option: 'finish_now',
  expires_at: new Date(Date.now() + 30_000).toISOString(),
};

const CONTEXT_CONSTRAINT: PendingConstraint = {
  request_id: 'req-002',
  constraint: 'context_compression',
  context: 'Context window is nearly full.',
  options: ['compress_continue', 'stop_here'],
  default_option: 'stop_here',
  expires_at: new Date(Date.now() + 30_000).toISOString(),
};

describe('DecisionCard — rendering', () => {
  it('renders the constraint context text', () => {
    const onDecide = vi.fn();
    render(<DecisionCard pending={TOOL_LIMIT_CONSTRAINT} onDecide={onDecide} />);
    expect(screen.getByText('You have used 10 tool iterations.')).toBeDefined();
  });

  it('renders one button per option', () => {
    const onDecide = vi.fn();
    render(<DecisionCard pending={TOOL_LIMIT_CONSTRAINT} onDecide={onDecide} />);
    // Two options: continue_10 and finish_now
    const buttons = screen.getAllByRole('button');
    // Filter to just the decision buttons (exclude any other interactive elements)
    const decisionButtons = buttons.filter(
      (b) => b.textContent === 'Continue (10 more)' || b.textContent === 'Finish now',
    );
    expect(decisionButtons).toHaveLength(2);
  });

  it('has a group role with accessible label', () => {
    const onDecide = vi.fn();
    render(<DecisionCard pending={TOOL_LIMIT_CONSTRAINT} onDecide={onDecide} />);
    // The component renders a div with role="group" and aria-label=title
    expect(screen.getByRole('group')).toBeDefined();
  });

  it('renders the "Remember this choice" checkbox', () => {
    const onDecide = vi.fn();
    render(<DecisionCard pending={TOOL_LIMIT_CONSTRAINT} onDecide={onDecide} />);
    expect(screen.getByRole('checkbox')).toBeDefined();
  });
});

describe('DecisionCard — onDecide callback', () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  it('calls onDecide with the clicked action_id and remember=false by default', () => {
    const onDecide = vi.fn();
    render(<DecisionCard pending={TOOL_LIMIT_CONSTRAINT} onDecide={onDecide} />);
    fireEvent.click(screen.getByText('Continue (10 more)'));
    expect(onDecide).toHaveBeenCalledTimes(1);
    expect(onDecide).toHaveBeenCalledWith('continue_10', false);
  });

  it('calls onDecide with remember=true when checkbox is ticked first', () => {
    const onDecide = vi.fn();
    render(<DecisionCard pending={TOOL_LIMIT_CONSTRAINT} onDecide={onDecide} />);
    fireEvent.click(screen.getByRole('checkbox'));
    fireEvent.click(screen.getByText('Finish now'));
    expect(onDecide).toHaveBeenCalledWith('finish_now', true);
  });

  it('only fires onDecide once even if a button is clicked twice (decide-once guard)', () => {
    const onDecide = vi.fn();
    render(<DecisionCard pending={TOOL_LIMIT_CONSTRAINT} onDecide={onDecide} />);
    fireEvent.click(screen.getByText('Continue (10 more)'));
    fireEvent.click(screen.getByText('Continue (10 more)'));
    expect(onDecide).toHaveBeenCalledTimes(1);
  });

  it('works for context_compression constraint', () => {
    const onDecide = vi.fn();
    render(<DecisionCard pending={CONTEXT_CONSTRAINT} onDecide={onDecide} />);
    fireEvent.click(screen.getByText('Compress and continue'));
    expect(onDecide).toHaveBeenCalledWith('compress_continue', false);
  });
});
