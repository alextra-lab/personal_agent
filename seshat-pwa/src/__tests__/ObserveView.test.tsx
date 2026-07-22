/**
 * Tests for ObserveView (ADR-0121 §5, FRE-920; FRE-938 server-fallback + no-em-dash fix).
 */

import { render, screen, waitFor } from '@testing-library/react';
import { vi, describe, it, expect, beforeEach } from 'vitest';

const mockGetSessionConfig = vi.fn();
const mockGetConfig = vi.fn();
const mockResolveLastSessionId = vi.fn();
let mockSearchParams = new URLSearchParams();

vi.mock('@/lib/agui-client', () => ({
  getSessionConfig: (...args: unknown[]) => mockGetSessionConfig(...args),
  getConfig: (...args: unknown[]) => mockGetConfig(...args),
}));

vi.mock('@/lib/session', () => ({
  LAST_SESSION_KEY: 'seshat_last_session_id',
  resolveLastSessionId: (...args: unknown[]) => mockResolveLastSessionId(...args),
}));

vi.mock('next/navigation', () => ({
  useSearchParams: () => mockSearchParams,
}));

import { ObserveView } from '@/components/ObserveView';

const LAST_SESSION_KEY = 'seshat_last_session_id';

const SESSION_CONFIG = {
  session_id: 's1',
  roles: {
    primary: {
      open: true,
      resolved: 'claude_sonnet',
      provenance: 'stored',
      candidates: [],
    },
    entity_extraction: { open: false },
  },
  providers: [
    { key: 'anthropic', placement: 'cloud' as const, available: true, summary: 'Anthropic API', max_concurrency: 50 },
    { key: 'slm_local', placement: 'local' as const, available: false, summary: 'SLM tunnel', max_concurrency: 2 },
  ],
};

// Post-FRE-938 shape: the sessionless config now resolves a catalog default
// for every role instead of omitting resolved/provenance.
const SESSIONLESS_CONFIG_WITH_DEFAULTS = {
  roles: {
    primary: { open: true, resolved: 'qwen3.6-35b-thinking', provenance: 'default', candidates: [] },
    entity_extraction: { open: false, resolved: 'claude_sonnet', provenance: 'default' },
  },
  providers: [],
};

beforeEach(() => {
  mockGetSessionConfig.mockReset();
  mockGetConfig.mockReset();
  mockResolveLastSessionId.mockReset();
  mockSearchParams = new URLSearchParams();
  localStorage.clear();
});

describe('ObserveView — with a session (query param)', () => {
  it('renders open and pinned roles with resolved bindings, no server fallback call', async () => {
    mockSearchParams = new URLSearchParams('session=s1');
    mockGetSessionConfig.mockResolvedValue(SESSION_CONFIG);

    render(<ObserveView />);

    expect(await screen.findByText('claude_sonnet')).toBeDefined();
    expect(screen.getByText('primary')).toBeDefined();
    expect(screen.getByText('entity_extraction')).toBeDefined();
    expect(screen.getByText('Open')).toBeDefined();
    expect(screen.getByText('Pinned')).toBeDefined();
    expect(mockResolveLastSessionId).not.toHaveBeenCalled(); // AC-5
  });

  it('renders the provider table with placement and availability', async () => {
    mockSearchParams = new URLSearchParams('session=s1');
    mockGetSessionConfig.mockResolvedValue(SESSION_CONFIG);

    render(<ObserveView />);

    expect(await screen.findByText('anthropic')).toBeDefined();
    expect(screen.getByText('slm_local')).toBeDefined();
    expect(screen.getByText('cloud')).toBeDefined();
    expect(screen.getByText('local')).toBeDefined();
  });
});

describe('ObserveView — localStorage key present (AC-5, no server fallback call)', () => {
  it('uses the stored session id directly without asking the server', async () => {
    localStorage.setItem(LAST_SESSION_KEY, 's1');
    mockGetSessionConfig.mockResolvedValue(SESSION_CONFIG);

    render(<ObserveView />);

    expect(await screen.findByText('claude_sonnet')).toBeDefined();
    expect(mockGetSessionConfig).toHaveBeenCalledWith('s1');
    expect(mockResolveLastSessionId).not.toHaveBeenCalled();
  });
});

describe('ObserveView — no query param, no localStorage key (FRE-938 fallback)', () => {
  it('renders a resolved model name (not an em-dash) from the sessionless catalog default (AC-3)', async () => {
    mockResolveLastSessionId.mockResolvedValue(undefined);
    mockGetConfig.mockResolvedValue(SESSIONLESS_CONFIG_WITH_DEFAULTS);

    render(<ObserveView />);

    expect(await screen.findByText('qwen3.6-35b-thinking')).toBeDefined();
    expect(screen.queryByText('—')).toBeNull();
    expect(
      screen.getByText(/No active conversation — showing catalog defaults/),
    ).toBeDefined();
  });

  it('asks the server for the most recent session before falling back to sessionless', async () => {
    mockResolveLastSessionId.mockResolvedValue(undefined);
    mockGetConfig.mockResolvedValue(SESSIONLESS_CONFIG_WITH_DEFAULTS);

    render(<ObserveView />);

    await waitFor(() => expect(mockResolveLastSessionId).toHaveBeenCalled());
  });

  it('upgrades to the session-scoped config once the server resolves a recent session', async () => {
    mockResolveLastSessionId.mockResolvedValue('server-resolved-session');
    mockGetConfig.mockResolvedValue(SESSIONLESS_CONFIG_WITH_DEFAULTS);
    mockGetSessionConfig.mockResolvedValue(SESSION_CONFIG);

    render(<ObserveView />);

    await waitFor(() =>
      expect(mockGetSessionConfig).toHaveBeenCalledWith('server-resolved-session'),
    );
    expect(await screen.findByText('claude_sonnet')).toBeDefined();
  });

  it('mints no session and shows catalog defaults when the user genuinely has none', async () => {
    mockResolveLastSessionId.mockResolvedValue(undefined);
    mockGetConfig.mockResolvedValue(SESSIONLESS_CONFIG_WITH_DEFAULTS);

    render(<ObserveView />);

    expect(
      await screen.findByText(/No active conversation — showing catalog defaults/),
    ).toBeDefined();
    expect(mockGetSessionConfig).not.toHaveBeenCalled();
  });
});
