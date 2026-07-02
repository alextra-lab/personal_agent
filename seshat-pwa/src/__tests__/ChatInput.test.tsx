/**
 * Tests for ChatInput Send↔Stop behaviour (ADR-0076 / FRE-400 WS2).
 *
 * Covers: Send button present when not streaming; Stop button (aria-label
 * "Stop generating") present when isStreaming=true; onStop fires on click;
 * textarea stays writable during streaming (FRE-421); Send gated on
 * pathUnavailable.
 */

import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { vi, describe, it, expect, beforeEach } from 'vitest';

// Mock agui-client (needed by useInferenceStatus transitively, and by the
// upload flow exercised in the FRE-692 override tests below).
vi.mock('@/lib/agui-client', () => ({
  SESHAT_API: 'http://localhost:9000',
  authHeaders: () => ({}),
  presignUpload: vi.fn(),
  uploadToR2: vi.fn(),
  completeUpload: vi.fn(),
}));

// Mock useInferenceStatus to return 'up' by default so Send is enabled.
vi.mock('@/hooks/useInferenceStatus', () => ({
  useInferenceStatus: vi.fn(() => ({ status: 'up', latencyMs: 10 })),
}));

import { ChatInput } from '@/components/ChatInput';
import { useInferenceStatus } from '@/hooks/useInferenceStatus';
import { presignUpload, uploadToR2, completeUpload } from '@/lib/agui-client';

const mockUseInference = useInferenceStatus as ReturnType<typeof vi.fn>;
const mockPresignUpload = presignUpload as ReturnType<typeof vi.fn>;
const mockUploadToR2 = uploadToR2 as ReturnType<typeof vi.fn>;
const mockCompleteUpload = completeUpload as ReturnType<typeof vi.fn>;

const DEFAULT_PROPS = {
  onSend: vi.fn(),
  profile: 'local' as const,
  onProfileChange: vi.fn(),
};

beforeEach(() => {
  DEFAULT_PROPS.onSend.mockClear();
  DEFAULT_PROPS.onProfileChange.mockClear();
  mockUseInference.mockReturnValue({ status: 'up', latencyMs: 10 });
  mockPresignUpload.mockReset();
  mockUploadToR2.mockReset();
  mockCompleteUpload.mockReset();
});

/** Drives a file through the mocked presign→upload→complete flow to a 'complete' chip. */
async function attachFile(container: HTMLElement, file: File, artifactId: string) {
  mockPresignUpload.mockResolvedValue({
    artifact_id: artifactId,
    upload_url: 'http://r2.example/put',
    expires_in: 60,
  });
  mockUploadToR2.mockResolvedValue(undefined);
  mockCompleteUpload.mockResolvedValue({
    artifact_id: artifactId,
    content_type: file.type,
    title: file.name,
  });

  const fileInput = container.querySelector('input[type="file"]') as HTMLInputElement;
  fireEvent.change(fileInput, { target: { files: [file] } });
  await waitFor(() => expect(screen.getByText(file.name)).toBeDefined());
}

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
    expect(DEFAULT_PROPS.onSend).toHaveBeenCalledWith('hello', []);
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

describe('ChatInput — per-attachment processing-target override (FRE-692, ADR-0101 §8a)', () => {
  function sendWithText(container: HTMLElement) {
    const textarea = screen.getByPlaceholderText('Message Seshat...');
    fireEvent.change(textarea, { target: { value: 'look at this' } });
    fireEvent.submit(container.querySelector('form')!);
  }

  it('sends processing_target "none" for an image attachment with no override chosen', async () => {
    const { container } = render(<ChatInput {...DEFAULT_PROPS} isStreaming={false} />);
    const file = new File(['x'], 'photo.png', { type: 'image/png' });
    await attachFile(container, file, 'a1');

    sendWithText(container);

    expect(DEFAULT_PROPS.onSend).toHaveBeenCalledWith('look at this', [
      { artifact_id: 'a1', content_type: 'image/png', title: 'photo.png', processing_target: 'none' },
    ]);
  });

  it('cycles Auto -> Cloud and sends processing_target "cloud"', async () => {
    const { container } = render(<ChatInput {...DEFAULT_PROPS} isStreaming={false} />);
    const file = new File(['x'], 'photo.png', { type: 'image/png' });
    await attachFile(container, file, 'a1');

    const toggle = screen.getByLabelText(/Set processing target for photo\.png, currently Auto/);
    fireEvent.click(toggle);
    expect(screen.getByLabelText(/currently Cloud/)).toBeDefined();

    sendWithText(container);

    expect(DEFAULT_PROPS.onSend).toHaveBeenCalledWith('look at this', [
      { artifact_id: 'a1', content_type: 'image/png', title: 'photo.png', processing_target: 'cloud' },
    ]);
  });

  it('cycles Auto -> Cloud -> Local and sends processing_target "local"', async () => {
    const { container } = render(<ChatInput {...DEFAULT_PROPS} isStreaming={false} />);
    const file = new File(['x'], 'photo.png', { type: 'image/png' });
    await attachFile(container, file, 'a1');

    const toggle = screen.getByLabelText(/Set processing target for photo\.png/);
    fireEvent.click(toggle); // -> Cloud
    fireEvent.click(screen.getByLabelText(/currently Cloud/)); // -> Local
    expect(screen.getByLabelText(/currently Local/)).toBeDefined();

    sendWithText(container);

    expect(DEFAULT_PROPS.onSend).toHaveBeenCalledWith('look at this', [
      { artifact_id: 'a1', content_type: 'image/png', title: 'photo.png', processing_target: 'local' },
    ]);
  });

  it('does not render the override control for a non-image attachment, and sends "none"', async () => {
    const { container } = render(<ChatInput {...DEFAULT_PROPS} isStreaming={false} />);
    const file = new File(['x'], 'notes.txt', { type: 'text/plain' });
    await attachFile(container, file, 'a2');

    expect(screen.queryByLabelText(/Set processing target/)).toBeNull();

    sendWithText(container);

    expect(DEFAULT_PROPS.onSend).toHaveBeenCalledWith('look at this', [
      { artifact_id: 'a2', content_type: 'text/plain', title: 'notes.txt', processing_target: 'none' },
    ]);
  });
});
