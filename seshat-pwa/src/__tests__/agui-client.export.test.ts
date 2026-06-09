/**
 * Tests for the artifact-export client helper (FRE-549).
 *
 * `fetchArtifactExport` wraps GET /api/v1/artifacts/{id}/export?mode=… —
 * the live FRE-530 endpoint. It must surface the exact HTTP status on the
 * thrown error so the UI can branch (502 "inline unavailable" vs. the rest),
 * and it must follow the existing PWA fetch pattern: `authHeaders()` only, no
 * `credentials` (the CF Access JWT is edge-injected — see agui-client.ts).
 */

import { vi, describe, it, expect, beforeEach, afterEach } from 'vitest';

import {
  fetchArtifactExport,
  ArtifactExportError,
} from '@/lib/agui-client';

const ARTIFACT_ID = 'abc-123';

function mockFetchOnce(init: { ok: boolean; status: number; blob?: Blob }) {
  const fn = vi.fn().mockResolvedValue({
    ok: init.ok,
    status: init.status,
    statusText: `status ${init.status}`,
    blob: vi.fn().mockResolvedValue(init.blob ?? new Blob(['x'], { type: 'text/html' })),
  });
  global.fetch = fn as unknown as typeof fetch;
  return fn;
}

describe('fetchArtifactExport', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('returns the blob on 200', async () => {
    const blob = new Blob(['<html></html>'], { type: 'text/html' });
    mockFetchOnce({ ok: true, status: 200, blob });
    const out = await fetchArtifactExport(ARTIFACT_ID, 'inline');
    expect(out).toBe(blob);
  });

  it('requests the inline mode with authHeaders and no credentials', async () => {
    const fn = mockFetchOnce({ ok: true, status: 200 });
    await fetchArtifactExport(ARTIFACT_ID, 'inline');
    const [url, opts] = fn.mock.calls[0];
    expect(String(url)).toContain(`/api/v1/artifacts/${ARTIFACT_ID}/export?mode=inline`);
    expect(opts).not.toHaveProperty('credentials');
  });

  it('requests the substitute mode', async () => {
    const fn = mockFetchOnce({ ok: true, status: 200 });
    await fetchArtifactExport(ARTIFACT_ID, 'substitute');
    expect(String(fn.mock.calls[0][0])).toContain('mode=substitute');
  });

  it('throws ArtifactExportError carrying status 400', async () => {
    mockFetchOnce({ ok: false, status: 400 });
    await expect(fetchArtifactExport(ARTIFACT_ID, 'inline')).rejects.toMatchObject({
      name: 'ArtifactExportError',
      status: 400,
    });
  });

  it('throws ArtifactExportError carrying status 502', async () => {
    mockFetchOnce({ ok: false, status: 502 });
    const err = await fetchArtifactExport(ARTIFACT_ID, 'inline').catch((e) => e);
    expect(err).toBeInstanceOf(ArtifactExportError);
    expect(err.status).toBe(502);
  });
});
