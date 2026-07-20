/**
 * Tests for ObserveView (ADR-0121 §5, FRE-920) — resolved bindings + provider table.
 */

import { render, screen } from '@testing-library/react';
import { vi, describe, it, expect, beforeEach } from 'vitest';

const mockGetSessionConfig = vi.fn();
const mockGetConfig = vi.fn();
let mockSearchParams = new URLSearchParams();

vi.mock('@/lib/agui-client', () => ({
  getSessionConfig: (...args: unknown[]) => mockGetSessionConfig(...args),
  getConfig: (...args: unknown[]) => mockGetConfig(...args),
}));

vi.mock('next/navigation', () => ({
  useSearchParams: () => mockSearchParams,
}));

import { ObserveView } from '@/components/ObserveView';

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

beforeEach(() => {
  mockGetSessionConfig.mockReset();
  mockGetConfig.mockReset();
  mockSearchParams = new URLSearchParams();
  localStorage.clear();
});

describe('ObserveView — with a session', () => {
  it('renders open and pinned roles with resolved bindings', async () => {
    mockSearchParams = new URLSearchParams('session=s1');
    mockGetSessionConfig.mockResolvedValue(SESSION_CONFIG);

    render(<ObserveView />);

    expect(await screen.findByText('claude_sonnet')).toBeDefined();
    expect(screen.getByText('primary')).toBeDefined();
    expect(screen.getByText('entity_extraction')).toBeDefined();
    expect(screen.getByText('Open')).toBeDefined();
    expect(screen.getByText('Pinned')).toBeDefined();
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

describe('ObserveView — no session (brand-new conversation)', () => {
  it('falls back to the sessionless config and flags it as not hydrated', async () => {
    mockGetConfig.mockResolvedValue({
      roles: { primary: { open: true, candidates: [] } },
      providers: [],
    });

    render(<ObserveView />);

    expect(
      await screen.findByText(/No active conversation — showing catalog defaults/),
    ).toBeDefined();
    expect(mockGetSessionConfig).not.toHaveBeenCalled();
  });
});
