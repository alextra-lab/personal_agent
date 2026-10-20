/**
 * Wire format of the Seshat effort level (ADR-0157 D2, FRE-1623).
 *
 * `POST /chat/stream` carries it as the form field `seshat_effort`. The write is
 * `PATCH /api/v1/sessions/{id}/effort` with the JSON body `{"seshat_effort": level}`.
 */

import { vi, describe, it, expect, beforeEach } from 'vitest';

import { sendChatMessage, setSessionEffort } from '@/lib/agui-client';

function mockFetch(status = 200) {
  const fn = vi.fn().mockResolvedValue({ ok: status < 400, status, statusText: 'x' });
  global.fetch = fn as unknown as typeof fetch;
  return fn;
}

beforeEach(() => {
  vi.restoreAllMocks();
});

describe('sendChatMessage — seshat_effort', () => {
  it('sends the level as the form field seshat_effort', async () => {
    const fn = mockFetch();
    await sendChatMessage({ message: 'hi', sessionId: 's1', seshatEffort: 'deep' });

    const [, opts] = fn.mock.calls[0];
    expect(new URLSearchParams(opts.body as string).get('seshat_effort')).toBe('deep');
  });

  it('sends no field when no level is given', async () => {
    const fn = mockFetch();
    await sendChatMessage({ message: 'hi', sessionId: 's1' });

    const [, opts] = fn.mock.calls[0];
    expect(new URLSearchParams(opts.body as string).has('seshat_effort')).toBe(false);
  });
});

describe('setSessionEffort', () => {
  it('sends one PATCH to the effort path with the level as JSON', async () => {
    const fn = mockFetch();
    await setSessionEffort('s 1', 'fast');

    expect(fn).toHaveBeenCalledTimes(1);
    const [url, opts] = fn.mock.calls[0];
    expect(url).toMatch(/\/api\/v1\/sessions\/s%201\/effort$/);
    expect(opts.method).toBe('PATCH');
    expect(JSON.parse(opts.body as string)).toEqual({ seshat_effort: 'fast' });
  });

  it('throws an error that ends with the status, so a caller can read a 404', async () => {
    mockFetch(404);
    await expect(setSessionEffort('s1', 'fast')).rejects.toThrow(/: 404$/);
  });
});
