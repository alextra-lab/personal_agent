/**
 * Tests for sendChatMessage's attachment serialization (ADR-0101 §8a / FRE-692).
 *
 * `sendChatMessage` must forward the `attachments` array — including each
 * item's `processing_target` — into the `/chat/stream` form body unchanged.
 * No transform happens between ChatInput's payload and the wire request.
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

describe('sendChatMessage — attachment processing_target passthrough', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('serializes a chosen processing_target unchanged into the form body', async () => {
    const fn = mockFetchOnce();
    const attachments: UploadedAttachment[] = [
      { artifact_id: 'a1', content_type: 'image/png', title: 'photo.png', processing_target: 'cloud' },
    ];

    await sendChatMessage({ message: 'hi', sessionId: 's1', attachments });

    const [, opts] = fn.mock.calls[0];
    const body = new URLSearchParams(opts.body as string);
    expect(JSON.parse(body.get('attachments')!)).toEqual(attachments);
  });

  it('serializes the default "none" processing_target when no override was chosen', async () => {
    const fn = mockFetchOnce();
    const attachments: UploadedAttachment[] = [
      { artifact_id: 'a1', content_type: 'image/png', title: 'photo.png', processing_target: 'none' },
    ];

    await sendChatMessage({ message: 'hi', sessionId: 's1', attachments });

    const [, opts] = fn.mock.calls[0];
    const body = new URLSearchParams(opts.body as string);
    expect(JSON.parse(body.get('attachments')!)[0].processing_target).toBe('none');
  });
});
