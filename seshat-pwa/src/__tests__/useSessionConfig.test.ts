/**
 * Tests for useSessionConfig (ADR-0121 §3, FRE-920).
 *
 * Covers the session-scoped read, the sessionless fallback for a brand-new
 * conversation (codex plan-review finding #1), and the `hydrated` flag that
 * distinguishes the two.
 */

import { renderHook, waitFor } from '@testing-library/react';
import { vi, describe, it, expect, beforeEach } from 'vitest';

const mockGetSessionConfig = vi.fn();
const mockGetConfig = vi.fn();

vi.mock('@/lib/agui-client', () => ({
  getSessionConfig: (...args: unknown[]) => mockGetSessionConfig(...args),
  getConfig: (...args: unknown[]) => mockGetConfig(...args),
}));

import { useSessionConfig } from '@/hooks/useSessionConfig';

const SESSION_CONFIG = {
  session_id: 's1',
  roles: {
    primary: { open: true, resolved: 'claude_sonnet', provenance: 'stored', candidates: [] },
  },
  providers: [{ key: 'anthropic', placement: 'cloud' as const, available: true, summary: null, max_concurrency: 50 }],
};

const SESSIONLESS_CONFIG = {
  roles: {
    primary: { open: true, candidates: [] },
  },
  providers: [{ key: 'anthropic', placement: 'cloud' as const, available: true, summary: null, max_concurrency: 50 }],
};

beforeEach(() => {
  mockGetSessionConfig.mockReset();
  mockGetConfig.mockReset();
});

describe('useSessionConfig — session-scoped read', () => {
  it('loads the session-scoped config and marks it hydrated', async () => {
    mockGetSessionConfig.mockResolvedValue(SESSION_CONFIG);

    const { result } = renderHook(() => useSessionConfig('s1'));

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.hydrated).toBe(true);
    expect(result.current.roles.primary?.resolved).toBe('claude_sonnet');
    expect(mockGetConfig).not.toHaveBeenCalled();
  });
});

describe('useSessionConfig — sessionless fallback', () => {
  it('falls back to the sessionless config on a 404 (brand-new conversation)', async () => {
    mockGetSessionConfig.mockResolvedValue(null);
    mockGetConfig.mockResolvedValue(SESSIONLESS_CONFIG);

    const { result } = renderHook(() => useSessionConfig('brand-new-session'));

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.hydrated).toBe(false);
    expect(result.current.roles.primary?.open).toBe(true);
    expect(result.current.roles.primary?.resolved).toBeUndefined();
  });

  it('uses the sessionless config directly when there is no sessionId at all', async () => {
    mockGetConfig.mockResolvedValue(SESSIONLESS_CONFIG);

    const { result } = renderHook(() => useSessionConfig(undefined));

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(mockGetSessionConfig).not.toHaveBeenCalled();
    expect(result.current.hydrated).toBe(false);
    expect(result.current.providers).toHaveLength(1);
  });
});

describe('useSessionConfig — refetch', () => {
  it('refetch() re-runs the load and reflects new data', async () => {
    mockGetSessionConfig.mockResolvedValue(SESSION_CONFIG);
    const { result } = renderHook(() => useSessionConfig('s1'));
    await waitFor(() => expect(result.current.loading).toBe(false));

    const updated = {
      ...SESSION_CONFIG,
      roles: {
        primary: { open: true, resolved: 'qwen3.6-35b-thinking', provenance: 'stored', candidates: [] },
      },
    };
    mockGetSessionConfig.mockResolvedValue(updated);

    result.current.refetch();

    await waitFor(() =>
      expect(result.current.roles.primary?.resolved).toBe('qwen3.6-35b-thinking'),
    );
  });
});
