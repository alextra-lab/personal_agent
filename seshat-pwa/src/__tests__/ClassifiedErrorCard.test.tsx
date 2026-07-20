/**
 * Tests for ClassifiedErrorCard component (FRE-398 / FRE-400 WS2).
 */

import { render, screen, fireEvent } from '@testing-library/react';
import { vi, describe, it, expect, beforeEach } from 'vitest';

vi.mock('@/lib/agui-client', () => ({
  SESHAT_API: 'http://localhost:9000',
  authHeaders: () => ({}),
}));

import { ClassifiedErrorCard } from '@/components/ClassifiedErrorCard';
import type { ClassifiedErrorData } from '@/lib/types';

const MODEL_SERVER_ERROR: ClassifiedErrorData = {
  category: 'model_server',
  reason: 'The model server returned an error.',
  next_step: 'Check that the model server is running.',
  actions: ['retry', 'stop'],
  partial: false,
};

const BUDGET_DENIED_ERROR: ClassifiedErrorData = {
  category: 'budget_denied',
  reason: 'Daily budget cap reached.',
  next_step: 'Try again tomorrow or reduce usage.',
  actions: ['stop'],
  partial: false,
};

const PARTIAL_ERROR: ClassifiedErrorData = {
  category: 'timeout',
  reason: 'Request timed out.',
  next_step: 'Retry with a simpler query.',
  actions: ['retry', 'stop'],
  partial: true,
};

describe('ClassifiedErrorCard — rendering', () => {
  it('renders with role="alert"', () => {
    render(<ClassifiedErrorCard error={MODEL_SERVER_ERROR} onDismiss={vi.fn()} />);
    expect(screen.getByRole('alert')).toBeDefined();
  });

  it('shows the correct title for model_server category', () => {
    render(<ClassifiedErrorCard error={MODEL_SERVER_ERROR} onDismiss={vi.fn()} />);
    expect(screen.getByText('Model server error')).toBeDefined();
  });

  it('shows the correct title for budget_denied category', () => {
    render(<ClassifiedErrorCard error={BUDGET_DENIED_ERROR} onDismiss={vi.fn()} />);
    expect(screen.getByText('Budget cap reached')).toBeDefined();
  });

  it('shows the error reason text', () => {
    render(<ClassifiedErrorCard error={MODEL_SERVER_ERROR} onDismiss={vi.fn()} />);
    expect(screen.getByText('The model server returned an error.')).toBeDefined();
  });

  it('shows the next_step guidance', () => {
    render(<ClassifiedErrorCard error={MODEL_SERVER_ERROR} onDismiss={vi.fn()} />);
    expect(screen.getByText('Check that the model server is running.')).toBeDefined();
  });

  it('shows the partial results notice when partial=true', () => {
    render(<ClassifiedErrorCard error={PARTIAL_ERROR} onDismiss={vi.fn()} />);
    expect(screen.getByText(/Partial results/)).toBeDefined();
  });

  it('does not show the partial notice when partial=false', () => {
    render(<ClassifiedErrorCard error={MODEL_SERVER_ERROR} onDismiss={vi.fn()} />);
    expect(screen.queryByText(/Partial results/)).toBeNull();
  });
});

describe('ClassifiedErrorCard — action buttons', () => {
  it('renders a Retry button when retry is in actions', () => {
    render(<ClassifiedErrorCard error={MODEL_SERVER_ERROR} onDismiss={vi.fn()} />);
    expect(screen.getByText('Retry')).toBeDefined();
  });

  it('calls onRetry when Retry is clicked', () => {
    const onRetry = vi.fn();
    const onDismiss = vi.fn();
    render(<ClassifiedErrorCard error={MODEL_SERVER_ERROR} onRetry={onRetry} onDismiss={onDismiss} />);
    fireEvent.click(screen.getByText('Retry'));
    expect(onRetry).toHaveBeenCalledTimes(1);
    expect(onDismiss).not.toHaveBeenCalled();
  });

  it('calls onDismiss when Dismiss (stop) button is clicked', () => {
    const onDismiss = vi.fn();
    render(<ClassifiedErrorCard error={MODEL_SERVER_ERROR} onDismiss={onDismiss} />);
    fireEvent.click(screen.getByText('Dismiss'));
    expect(onDismiss).toHaveBeenCalledTimes(1);
  });

  it('falls back to onDismiss for retry when onRetry is not provided', () => {
    const onDismiss = vi.fn();
    render(<ClassifiedErrorCard error={MODEL_SERVER_ERROR} onDismiss={onDismiss} />);
    fireEvent.click(screen.getByText('Retry'));
    expect(onDismiss).toHaveBeenCalledTimes(1);
  });
});
