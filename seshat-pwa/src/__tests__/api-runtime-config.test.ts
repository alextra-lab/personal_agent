/**
 * Tests for GET /api/runtime-config (FRE-339).
 *
 * Verifies the route handler returns seshat_url from SESHAT_URL env var,
 * does NOT return gateway_token (security: bearer token not over unauth endpoint),
 * and defaults to localhost when env absent.
 */

import { vi, describe, it, expect, beforeEach, afterEach } from 'vitest';

const mockJsonFn = vi.fn();

vi.mock('next/server', () => ({
  NextResponse: {
    json: mockJsonFn.mockImplementation((data: unknown) => ({ data })),
  },
}));

describe('GET /api/runtime-config', () => {
  const originalEnv = process.env;

  beforeEach(() => {
    process.env = { ...originalEnv };
    mockJsonFn.mockClear();
  });

  afterEach(() => {
    process.env = originalEnv;
    vi.resetModules();
  });

  it('returns seshat_url from SESHAT_URL env var', async () => {
    process.env.SESHAT_URL = 'https://agent.example.com';
    const { GET } = await import('@/app/api/runtime-config/route');
    GET();
    expect(mockJsonFn).toHaveBeenCalledWith(
      expect.objectContaining({ seshat_url: 'https://agent.example.com' }),
    );
  });

  it('defaults to localhost:9000 when SESHAT_URL is absent', async () => {
    delete process.env.SESHAT_URL;
    const { GET } = await import('@/app/api/runtime-config/route');
    GET();
    expect(mockJsonFn).toHaveBeenCalledWith(
      expect.objectContaining({ seshat_url: 'http://localhost:9000' }),
    );
  });

  it('does NOT include gateway_token in the response', async () => {
    process.env.SESHAT_URL = 'https://agent.example.com';
    process.env.GATEWAY_TOKEN = 'super-secret';
    const { GET } = await import('@/app/api/runtime-config/route');
    GET();
    const payload = mockJsonFn.mock.calls[0][0] as Record<string, unknown>;
    expect(payload).not.toHaveProperty('gateway_token');
  });
});
