/**
 * Tests for the share-link client helpers (FRE-1784, ADR-0153 Amendment 1 L9).
 *
 * The gateway endpoints are in src/personal_agent/service/share_links_router.py. A failure must
 * carry the API's own error text (`detail`), so the share sheet can show it (FRE-1784 AC-4).
 */

import { vi, describe, it, expect, afterEach } from 'vitest';

import {
  ShareLinkError,
  createShareLink,
  listShareLinks,
  revokeShareLink,
} from '@/lib/agui-client';

function mockFetchOnce(status: number, body: unknown = null) {
  const fn = vi.fn().mockResolvedValue({
    ok: status >= 200 && status < 300,
    status,
    json: vi.fn().mockImplementation(async () => {
      if (body === null) throw new SyntaxError('no body');
      return body;
    }),
  });
  global.fetch = fn as unknown as typeof fetch;
  return fn;
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe('createShareLink', () => {
  it('POSTs the expiry choice as JSON and returns the created link', async () => {
    const created = {
      link_id: 'l-1',
      url: 'https://share.example.com/s/tok',
      expires_at: '2026-12-01T00:00:00Z',
    };
    const fn = mockFetchOnce(201, created);
    const out = await createShareLink('a-1', 7);
    expect(out).toEqual(created);
    const [url, opts] = fn.mock.calls[0];
    expect(String(url)).toMatch(/\/api\/v1\/artifacts\/a-1\/share-links$/);
    expect(opts.method).toBe('POST');
    expect(opts.headers['Content-Type']).toBe('application/json');
    expect(JSON.parse(opts.body)).toEqual({ expires_in_days: 7 });
    expect(opts).not.toHaveProperty('credentials');
  });

  it.each([
    [400, { detail: 'this item cannot be shared by link' }, 'this item cannot be shared by link'],
    [
      422,
      { detail: 'this artifact loads files from the artifact host that a link cannot reach' },
      'this artifact loads files from the artifact host that a link cannot reach',
    ],
    [502, { detail: 'Bad Gateway' }, 'Bad Gateway'],
    [503, { detail: 'Service Unavailable' }, 'Service Unavailable'],
  ])('a %i carries the API text', async (status, body, text) => {
    mockFetchOnce(status, body);
    const err = await createShareLink('a-1', 7).catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ShareLinkError);
    expect((err as ShareLinkError).status).toBe(status);
    expect((err as ShareLinkError).message).toBe(text);
  });

  it('joins the messages of a validation error list', async () => {
    mockFetchOnce(422, {
      detail: [{ loc: ['body'], msg: 'Value error, expires_in_days must be 1, 7 or 30' }],
    });
    const err = (await createShareLink('a-1', 7).catch((e: unknown) => e)) as ShareLinkError;
    expect(err.message).toBe('Value error, expires_in_days must be 1, 7 or 30');
  });

  it('names the status when the body has no text', async () => {
    mockFetchOnce(500, null);
    const err = (await createShareLink('a-1', 7).catch((e: unknown) => e)) as ShareLinkError;
    expect(err.status).toBe(500);
    expect(err.message).toBe('share link request failed: 500');
  });
});

describe('listShareLinks', () => {
  it('returns the items', async () => {
    const items = [
      {
        link_id: 'l-1',
        artifact_id: 'a-1',
        artifact_title: 'Report',
        created_at: '2026-11-24T10:00:00Z',
        expires_at: '2026-12-01T10:00:00Z',
        view_count: 3,
        last_viewed_at: null,
      },
    ];
    const fn = mockFetchOnce(200, { items });
    expect(await listShareLinks()).toEqual(items);
    expect(String(fn.mock.calls[0][0])).toMatch(/\/api\/v1\/share-links$/);
  });

  it('throws a ShareLinkError with the status on 403', async () => {
    mockFetchOnce(403, { detail: 'Forbidden' });
    const err = (await listShareLinks().catch((e: unknown) => e)) as ShareLinkError;
    expect(err).toBeInstanceOf(ShareLinkError);
    expect(err.status).toBe(403);
  });
});

describe('revokeShareLink', () => {
  it('sends a DELETE for the link', async () => {
    const fn = mockFetchOnce(204);
    await revokeShareLink('l-1');
    const [url, opts] = fn.mock.calls[0];
    expect(String(url)).toMatch(/\/api\/v1\/share-links\/l-1$/);
    expect(opts.method).toBe('DELETE');
  });

  it('throws on 404 with the API text', async () => {
    mockFetchOnce(404, { detail: 'Not Found' });
    const err = (await revokeShareLink('l-1').catch((e: unknown) => e)) as ShareLinkError;
    expect(err.status).toBe(404);
    expect(err.message).toBe('Not Found');
  });
});
