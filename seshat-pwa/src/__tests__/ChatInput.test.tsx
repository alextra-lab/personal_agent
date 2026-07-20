/**
 * Tests for ChatInput Send↔Stop behaviour (ADR-0076 / FRE-400 WS2) and
 * upload flow (FRE-369). ADR-0121 T5 (FRE-920) removed the profile pill and
 * the per-attachment processing-target override — model-picker interaction
 * is covered separately in ModelPicker.test.tsx.
 */

import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { vi, describe, it, expect, beforeEach } from 'vitest';

vi.mock('@/lib/agui-client', () => ({
  SESHAT_API: 'http://localhost:9000',
  authHeaders: () => ({}),
  presignUpload: vi.fn(),
  uploadToR2: vi.fn(),
  completeUpload: vi.fn(),
}));

import { ChatInput } from '@/components/ChatInput';
import { presignUpload, uploadToR2, completeUpload } from '@/lib/agui-client';

const mockPresignUpload = presignUpload as ReturnType<typeof vi.fn>;
const mockUploadToR2 = uploadToR2 as ReturnType<typeof vi.fn>;
const mockCompleteUpload = completeUpload as ReturnType<typeof vi.fn>;

const DEFAULT_PROPS = {
  onSend: vi.fn(),
  candidates: [],
  selectedModelKey: null,
  modelHydrated: false,
  onModelChange: vi.fn(),
};

beforeEach(() => {
  DEFAULT_PROPS.onSend.mockClear();
  DEFAULT_PROPS.onModelChange.mockClear();
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

describe('ChatInput — attachments (FRE-369)', () => {
  function sendWithText(container: HTMLElement) {
    const textarea = screen.getByPlaceholderText('Message Seshat...');
    fireEvent.change(textarea, { target: { value: 'look at this' } });
    fireEvent.submit(container.querySelector('form')!);
  }

  it('sends a completed attachment with no processing-target field (ADR-0121 T5)', async () => {
    const { container } = render(<ChatInput {...DEFAULT_PROPS} isStreaming={false} />);
    const file = new File(['x'], 'photo.png', { type: 'image/png' });
    await attachFile(container, file, 'a1');

    sendWithText(container);

    expect(DEFAULT_PROPS.onSend).toHaveBeenCalledWith('look at this', [
      { artifact_id: 'a1', content_type: 'image/png', title: 'photo.png' },
    ]);
  });

  it('renders no per-attachment override control (removed in ADR-0121 T5)', async () => {
    const { container } = render(<ChatInput {...DEFAULT_PROPS} isStreaming={false} />);
    const file = new File(['x'], 'photo.png', { type: 'image/png' });
    await attachFile(container, file, 'a1');

    expect(screen.queryByLabelText(/Set processing target/)).toBeNull();
  });

  it('Send is blocked while an upload is in progress', async () => {
    mockPresignUpload.mockReturnValue(new Promise(() => {})); // never resolves
    const { container } = render(<ChatInput {...DEFAULT_PROPS} isStreaming={false} />);
    const file = new File(['x'], 'photo.png', { type: 'image/png' });
    const fileInput = container.querySelector('input[type="file"]') as HTMLInputElement;
    fireEvent.change(fileInput, { target: { files: [file] } });

    const textarea = screen.getByPlaceholderText('Message Seshat...');
    fireEvent.change(textarea, { target: { value: 'hello' } });
    const sendBtn = screen.getByLabelText('Send message');
    expect(sendBtn).toBeDisabled();
  });
});

describe('ChatInput — model picker', () => {
  it('renders the ModelPicker control', () => {
    render(<ChatInput {...DEFAULT_PROPS} isStreaming={false} />);
    expect(screen.getByLabelText('Choose model')).toBeDefined();
  });
});
