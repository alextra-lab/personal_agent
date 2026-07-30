/**
 * ADR-0123 T4 (FRE-937) — ChatMessage's integration with the collapsed
 * per-turn summary. No test file existed for this component before this
 * ticket (codex plan review flagged the wiring as otherwise untested).
 */

import { render, screen } from '@testing-library/react';
import { describe, it, expect } from 'vitest';

import { ChatMessage } from '@/components/ChatMessage';
import type { ChatMessage as ChatMessageType } from '@/lib/types';

function message(overrides: Partial<ChatMessageType>): ChatMessageType {
  return {
    id: 'm1',
    role: 'assistant',
    content: 'Hello',
    timestamp: new Date('2026-07-30T10:00:00.000Z'),
    ...overrides,
  };
}

describe('ChatMessage — collapsed turn summary integration', () => {
  it('renders no summary control when phaseSummary is absent', () => {
    render(<ChatMessage message={message({})} />);
    expect(screen.queryByTestId('turn-summary')).toBeNull();
  });

  it('renders the TurnSummaryPanel when phaseSummary is present', () => {
    render(
      <ChatMessage
        message={message({
          phaseSummary: {
            phases: [
              { phaseId: 'p1', phase: 'planning', detail: null, durationMs: 5000, state: 'completed', parentId: null },
            ],
            tools: [],
            terminalState: 'completed',
          },
        })}
      />,
    );
    expect(screen.getByTestId('turn-summary')).toBeInTheDocument();
    expect(screen.getByTestId('turn-summary-header').textContent?.toLowerCase()).toContain('completed');
  });

  it('renders a cancelled-turn summary distinctly', () => {
    render(
      <ChatMessage
        message={message({
          content: '',
          phaseSummary: {
            phases: [
              { phaseId: 'p1', phase: 'planning', detail: null, durationMs: 5000, state: 'cancelled', parentId: null },
            ],
            tools: [],
            terminalState: 'cancelled',
          },
        })}
      />,
    );
    expect(screen.getByTestId('turn-summary-header').textContent?.toLowerCase()).toContain('cancelled');
  });

  it('does not render the copy button for an empty-content placeholder message', () => {
    render(
      <ChatMessage
        message={message({
          content: '',
          phaseSummary: {
            phases: [
              { phaseId: 'p1', phase: 'planning', detail: null, durationMs: 5000, state: 'error', parentId: null },
            ],
            tools: [],
            terminalState: 'error',
          },
        })}
      />,
    );
    expect(screen.queryByLabelText('Copy message')).toBeNull();
  });

  it('still renders the copy button for a normal, non-empty message', () => {
    render(<ChatMessage message={message({ content: 'hi there' })} />);
    expect(screen.getByLabelText('Copy message')).toBeInTheDocument();
  });
});
