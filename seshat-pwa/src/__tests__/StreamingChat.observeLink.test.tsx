/**
 * FRE-1754: the session drawer shows a link to Observe (Grafana) when
 * OBSERVE_URL is configured, and shows no such link when it is not.
 *
 * Renders the real StreamingChat inside the real RuntimeConfigProvider and
 * opens the drawer, so the test asserts what the owner sees.
 */

import { render, screen, cleanup, fireEvent, act } from '@testing-library/react';
import { vi, describe, it, expect, beforeEach, afterEach } from 'vitest';

vi.mock('next/navigation', () => ({ useRouter: () => ({ push: vi.fn() }) }));
vi.mock('next/link', () => ({
  default: ({ children, href }: { children: React.ReactNode; href: string }) => (
    <a href={href}>{children}</a>
  ),
}));

vi.mock('@/lib/agui-client', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/agui-client')>()),
  connectWebSocket: vi.fn(() => ({ send: vi.fn(), close: vi.fn() })),
  sendChatMessage: vi.fn().mockResolvedValue(undefined),
  getSessionMessages: vi.fn().mockResolvedValue([]),
  getSession: vi.fn().mockResolvedValue({ turn_count: 1, cost_usd: 0.09 }),
  listSessions: vi.fn().mockResolvedValue([]),
  setSessionSelection: vi.fn().mockResolvedValue(undefined),
}));

vi.mock('@/hooks/useSessionConfig', () => ({
  useSessionConfig: () => ({ roles: {}, hydrated: true, refetch: vi.fn() }),
}));

vi.mock('@/components/ChatInput', () => ({ ChatInput: () => <div /> }));

import { StreamingChat } from '@/components/StreamingChat';
import { RuntimeConfigProvider } from '@/components/RuntimeConfigProvider';

async function openDrawer(observeUrl: string | undefined) {
  render(
    <RuntimeConfigProvider seshatUrl="http://test:9000" gatewayToken="" observeUrl={observeUrl}>
      <StreamingChat sessionId="sess-a" />
    </RuntimeConfigProvider>,
  );
  await act(async () => {
    fireEvent.click(screen.getByLabelText('Open session list'));
  });
}

beforeEach(() => {
  Element.prototype.scrollIntoView = vi.fn();
  localStorage.clear();
});

afterEach(() => {
  cleanup();
});

describe('StreamingChat drawer — Observe (Grafana) link', () => {
  it('shows the link to the configured address when the drawer is open', async () => {
    await openDrawer('https://observe.example.com');
    const link = screen.getByRole('link', { name: /observe \(grafana\)/i });
    expect(link.getAttribute('href')).toBe('https://observe.example.com');
    expect(link.getAttribute('target')).toBe('_blank');
  });

  it('keeps the in-app Observe page link separate from the Grafana link', async () => {
    await openDrawer('https://observe.example.com');
    const inApp = screen.getByRole('link', { name: 'Observe' });
    expect(inApp.getAttribute('href')).toBe('/observe?session=sess-a');
  });

  it('shows no Grafana link when no address is configured', async () => {
    await openDrawer(undefined);
    expect(screen.queryByRole('link', { name: /grafana/i })).toBeNull();
    expect(screen.getByRole('link', { name: /Artifacts/ })).toBeTruthy();
  });
});
