/**
 * Tests for ChatInput Send↔Stop behaviour (ADR-0076 / FRE-400 WS2).
 *
 * Covers: Send button present when not streaming; Stop button (aria-label
 * "Stop generating") present when isStreaming=true; onStop fires on click;
 * textarea stays writable during streaming (FRE-421); Send gated on
 * pathUnavailable.
 */

import { render, screen, fireEvent } from '@testing-library/react';
import { vi, describe, it, expect, beforeEach } from 'vitest';

// Mock agui-client (needed by useInferenceStatus transitively).
vi.mock('@/lib/agui-client', () => ({
  SESHAT_API: 'http://localhost:9000',
  authHeaders: () => ({}),
}));

// Mock useInferenceStatus to return 'up' by default so Send is enabled.
vi.mock('@/hooks/useInferenceStatus', () => ({
  useInferenceStatus: vi.fn(() => ({ status: 'up', latencyMs: 10 })),
}));

import { ChatInput } from '@/components/ChatInput';
import { useInferenceStatus } from '@/hooks/useInferenceStatus';

const mockUseInference = useInferenceStatus as ReturnType<typeof vi.fn>;

const DEFAULT_PROPS = {
  onSend: vi.fn(),
  profile: 'local' as const,
  onProfileChange: vi.fn(),
};

beforeEach(() => {
  DEFAULT_PROPS.onSend.mockClear();
  DEFAULT_PROPS.onProfileChange.mockClear();
  mockUseInference.mockReturnValue({ status: 'up', latencyMs: 10 });
});

describe('ChatInput — Send button (not streaming)', () => {
  it('renders Send button when isStreaming is false', () => {
    render(<ChatInput {...DEFAULT_PROPS} isStreaming={false} />);
    expect(screen.getByLabelText('Send message')).toBeDefined();
    expect(screen.queryByLabelText('Stop generating')).toBeNull();
  });

  it('calls onSend with trimmed text on form submit', () => {
    render(<ChatInput {...DEFAULT_PROPS} isStreaming={false} />);
    const textarea = screen.getByPlaceholderText('Message Seshat...');
    fireEvent.change(textarea, { target: { value: '  hello  ' } });
    fireEvent.submit(textarea.closest('form')!);
    expect(DEFAULT_PROPS.onSend).toHaveBeenCalledWith('hello');
  });

  it('does not call onSend when text is empty', () => {
    const { container } = render(<ChatInput {...DEFAULT_PROPS} isStreaming={false} />);
    const form = container.querySelector('form')!;
    fireEvent.submit(form);
    expect(DEFAULT_PROPS.onSend).not.toHaveBeenCalled();
  });
});

describe('ChatInput — Stop button (isStreaming=true)', () => {
  it('renders Stop button instead of Send when isStreaming=true', () => {
    render(<ChatInput {...DEFAULT_PROPS} isStreaming={true} onStop={vi.fn()} />);
    expect(screen.getByLabelText('Stop generating')).toBeDefined();
    expect(screen.queryByLabelText('Send message')).toBeNull();
  });

  it('calls onStop when Stop button is clicked', () => {
    const onStop = vi.fn();
    render(<ChatInput {...DEFAULT_PROPS} isStreaming={true} onStop={onStop} />);
    fireEvent.click(screen.getByLabelText('Stop generating'));
    expect(onStop).toHaveBeenCalledTimes(1);
  });

  it('textarea is NOT disabled during streaming (FRE-421)', () => {
    render(<ChatInput {...DEFAULT_PROPS} isStreaming={true} onStop={vi.fn()} />);
    const textarea = screen.getByPlaceholderText('Message Seshat...');
    // The textarea must remain writable so the user can compose while waiting.
    expect(textarea).not.toBeDisabled();
  });
});

describe('ChatInput — path unavailable gating (FRE-421)', () => {
  it('shows an unavailability notice when the active path is down', () => {
    mockUseInference.mockReturnValue({ status: 'down', latencyMs: null });
    render(<ChatInput {...DEFAULT_PROPS} isStreaming={false} />);
    expect(screen.getByText(/currently unavailable/)).toBeDefined();
  });

  it('Send button is disabled when text present but path is down', () => {
    mockUseInference.mockReturnValue({ status: 'down', latencyMs: null });
    render(<ChatInput {...DEFAULT_PROPS} isStreaming={false} />);
    const textarea = screen.getByPlaceholderText('Message Seshat...');
    fireEvent.change(textarea, { target: { value: 'hello' } });
    const sendBtn = screen.getByLabelText('Send message');
    expect(sendBtn).toBeDisabled();
  });
});
