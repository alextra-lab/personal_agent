/**
 * Tests for sendChatMessage's attachment serialization (FRE-369).
 *
 * `sendChatMessage` must forward the `attachments` array into the
 * `/chat/stream` form body unchanged. No transform happens between
 * ChatInput's payload and the wire request. ADR-0121 T5 (FRE-920) removed
 * `processing_target` — vision is a pinned role with no per-attachment
 * choice to pass through.
 */

import { vi, describe, it, expect, beforeEach, afterEach } from 'vitest';

import { sendChatMessage, type UploadedAttachment } from '@/lib/agui-client';

function mockFetchOnce() {
  const fn = vi.fn().mockResolvedValue({
    ok: true,
    status: 200,
  });
  global.fetch = fn as unknown as typeof fetch;
  return fn;
}

describe('sendChatMessage — attachment passthrough', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('serializes attachments unchanged into the form body', async () => {
    const fn = mockFetchOnce();
    const attachments: UploadedAttachment[] = [
      { artifact_id: 'a1', content_type: 'image/png', title: 'photo.png' },
    ];

    await sendChatMessage({ message: 'hi', sessionId: 's1', attachments });

    const [, opts] = fn.mock.calls[0];
    const body = new URLSearchParams(opts.body as string);
    expect(JSON.parse(body.get('attachments')!)).toEqual(attachments);
  });

  it('sends primary_selection when provided', async () => {
    const fn = mockFetchOnce();
    await sendChatMessage({ message: 'hi', sessionId: 's1', primarySelection: 'claude_sonnet' });

    const [, opts] = fn.mock.calls[0];
    const body = new URLSearchParams(opts.body as string);
    expect(body.get('primary_selection')).toBe('claude_sonnet');
  });

  it('omits primary_selection when not provided', async () => {
    const fn = mockFetchOnce();
    await sendChatMessage({ message: 'hi', sessionId: 's1' });

    const [, opts] = fn.mock.calls[0];
    const body = new URLSearchParams(opts.body as string);
    expect(body.has('primary_selection')).toBe(false);
  });
});
