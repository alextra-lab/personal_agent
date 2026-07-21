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
import type { DeploymentView, PendingConstraint } from '@/lib/types';

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

// ADR-0122 T3 (FRE-921): the artifact_builder constraint's options are
// catalog deployment keys, not fixed action ids — `claude_haiku` deliberately
// has no matching candidate below, to exercise the fallback-to-plain-label path.
const BUILDER_CONSTRAINT: PendingConstraint = {
  request_id: 'req-003',
  constraint: 'artifact_builder',
  context: 'Choose the model to build "Q3 dashboard".',
  options: ['claude_opus', 'claude_haiku'],
  default_option: 'claude_sonnet',
  expires_at: new Date(Date.now() + 30_000).toISOString(),
};

const CLAUDE_OPUS_CANDIDATE: DeploymentView = {
  key: 'claude_opus',
  id: 'claude-opus-4-8',
  provider: 'anthropic',
  placement: 'cloud',
  kind: 'llm',
  status: 'available',
  summary: 'Best for dense, interactive artifacts.',
  context_length: 200_000,
  max_tokens: 32_000,
  supports_vision: true,
  supports_pdf_document: true,
  input_cost_per_token: 0.000015,
  output_cost_per_token: 0.000075,
};

const LOCAL_FREE_CANDIDATE: DeploymentView = {
  key: 'local_free',
  id: 'local-free',
  provider: 'ollama',
  placement: 'local',
  kind: 'llm',
  status: 'available',
  summary: 'Runs on your machine, free.',
  context_length: 8_192,
  max_tokens: null,
  supports_vision: false,
  supports_pdf_document: false,
  input_cost_per_token: null,
  output_cost_per_token: null,
};

const BUILDER_CANDIDATES: DeploymentView[] = [CLAUDE_OPUS_CANDIDATE];

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

describe('DecisionCard — artifact_builder catalog detail (ADR-0122 T3 / FRE-921)', () => {
  it('renders catalog detail for a builder option with a matching candidate', () => {
    const onDecide = vi.fn();
    render(
      <DecisionCard
        pending={BUILDER_CONSTRAINT}
        onDecide={onDecide}
        builderCandidates={BUILDER_CANDIDATES}
      />,
    );
    expect(screen.getByText('claude_opus')).toBeDefined();
    expect(screen.getByText(/anthropic/)).toBeDefined();
    expect(screen.getByText(/200K context/)).toBeDefined();
    expect(screen.getByText(/32K max output/)).toBeDefined();
    expect(screen.getByText(/\$15\.00\/M in/)).toBeDefined();
    expect(screen.getByText(/\$75\.00\/M out/)).toBeDefined();
    expect(screen.getByText('Best for dense, interactive artifacts.')).toBeDefined();
  });

  it('falls back to the plain label pill when a builder option has no matching candidate', () => {
    const onDecide = vi.fn();
    render(
      <DecisionCard
        pending={BUILDER_CONSTRAINT}
        onDecide={onDecide}
        builderCandidates={BUILDER_CANDIDATES}
      />,
    );
    const haikuButton = screen.getByText('claude_haiku');
    expect(haikuButton).toBeDefined();
    // The fallback pill is a bare button whose only text is the raw id —
    // no nested catalog-detail span, unlike the enriched claude_opus button.
    expect(haikuButton.tagName).toBe('BUTTON');
    expect(haikuButton.querySelector('span')).toBeNull();
  });

  it('falls back to plain rendering entirely when builderCandidates is omitted', () => {
    const onDecide = vi.fn();
    render(<DecisionCard pending={BUILDER_CONSTRAINT} onDecide={onDecide} />);
    expect(screen.getByText('claude_opus')).toBeDefined();
    expect(screen.getByText('claude_haiku')).toBeDefined();
    expect(screen.queryByText(/context/)).toBeNull();
  });

  it("calls onDecide with the candidate's key when a detail button is clicked", () => {
    const onDecide = vi.fn();
    render(
      <DecisionCard
        pending={BUILDER_CONSTRAINT}
        onDecide={onDecide}
        builderCandidates={BUILDER_CANDIDATES}
      />,
    );
    fireEvent.click(screen.getByText('claude_opus'));
    expect(onDecide).toHaveBeenCalledWith('claude_opus', false);
  });

  it('ignores builderCandidates entirely for a non-artifact_builder constraint', () => {
    const onDecide = vi.fn();
    const collidingCandidates: DeploymentView[] = [
      { ...CLAUDE_OPUS_CANDIDATE, key: 'continue_10' },
    ];
    render(
      <DecisionCard
        pending={TOOL_LIMIT_CONSTRAINT}
        onDecide={onDecide}
        builderCandidates={collidingCandidates}
      />,
    );
    expect(screen.getByText('Continue (10 more)')).toBeDefined();
    expect(screen.queryByText(/M in/)).toBeNull();
    expect(screen.queryByText('Best for dense, interactive artifacts.')).toBeNull();
  });

  it('renders "provider default" and omits cost text for a candidate with null max_tokens/costs', () => {
    const onDecide = vi.fn();
    const pending: PendingConstraint = {
      ...BUILDER_CONSTRAINT,
      options: ['local_free'],
    };
    render(
      <DecisionCard pending={pending} onDecide={onDecide} builderCandidates={[LOCAL_FREE_CANDIDATE]} />,
    );
    expect(screen.getByText(/provider default max output/)).toBeDefined();
    expect(screen.queryByText(/\$NaN/)).toBeNull();
    expect(screen.queryByText(/\$0\.00/)).toBeNull();
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
