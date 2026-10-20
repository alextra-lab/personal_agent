/**
 * FRE-1623 (ADR-0157 D2, D5): the Seshat effort selector in the real chat screen.
 *
 * These tests render the real StreamingChat, ChatInput and EffortSelector, with only the
 * network layer mocked. They read what the owner sees and what the chat request carries.
 */

import { render, screen, act, cleanup, fireEvent, within, waitFor } from '@testing-library/react';
import { vi, describe, it, expect, beforeEach, afterEach } from 'vitest';
import type { Mock } from 'vitest';

let deliverToSocket: ((event: unknown) => void) | null = null;

vi.mock('next/navigation', () => ({ useRouter: () => ({ push: vi.fn() }) }));
vi.mock('next/link', () => ({
  default: ({ children, href }: { children: React.ReactNode; href: string }) => (
    <a href={href}>{children}</a>
  ),
}));

vi.mock('@/lib/agui-client', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/agui-client')>()),
  connectWebSocket: vi.fn((_sessionId: string, onEvent: (e: unknown) => void) => {
    deliverToSocket = onEvent;
    return { send: vi.fn(), close: vi.fn() };
  }),
  sendChatMessage: vi.fn().mockResolvedValue(undefined),
  getSessionMessages: vi.fn().mockResolvedValue([]),
  getSession: vi.fn(),
  getSessionConfig: vi.fn(),
  getConfig: vi.fn(),
  listSessions: vi.fn().mockResolvedValue([]),
  setSessionSelection: vi.fn().mockResolvedValue(undefined),
  setSessionEffort: vi.fn(),
}));

vi.mock('@/lib/submitTurnRating', () => ({
  submitTurnRating: vi.fn().mockResolvedValue(true),
}));

import { StreamingChat } from '@/components/StreamingChat';
import {
  getConfig,
  getSession,
  getSessionConfig,
  getSessionMessages,
  sendChatMessage,
  setSessionEffort,
} from '@/lib/agui-client';

const mockGetSession = getSession as Mock;
const mockGetSessionConfig = getSessionConfig as Mock;
const mockGetConfig = getConfig as Mock;
const mockGetSessionMessages = getSessionMessages as Mock;
const mockSendChatMessage = sendChatMessage as Mock;
const mockSetSessionEffort = setSessionEffort as Mock;

// The session's model declares `fast` and no `deep`.
const MODEL = {
  key: 'qwen3.8-flash-next',
  id: 'unsloth/qwen3.8-flash-next',
  provider: 'slm_local',
  placement: 'local',
  kind: 'llm',
  status: 'active',
  summary: null,
  context_length: 131072,
  max_tokens: 8192,
  supports_vision: true,
  supports_pdf_document: false,
  input_cost_per_token: null,
  output_cost_per_token: null,
  modes: ['default', 'fast'],
};

const CONFIG = {
  roles: { primary: { open: true, resolved: MODEL.key, candidates: [MODEL] } },
  providers: [],
  effort_levels: { fast: 'fast', standard: null, deep: 'deep' },
};

function mockExistingSession(level: string) {
  mockGetSession.mockResolvedValue({ session_id: 's1', seshat_effort: level, turn_count: 2 });
  mockGetSessionConfig.mockResolvedValue({ ...CONFIG, session_id: 's1', seshat_effort: level });
}

function mockNewSession() {
  mockGetSession.mockResolvedValue(null);
  mockGetSessionConfig.mockResolvedValue(null); // no row: the sessionless read serves the config
  mockGetConfig.mockResolvedValue(CONFIG);
}

const effortButton = () => screen.getByLabelText('Choose Seshat effort');

async function renderChat() {
  render(<StreamingChat sessionId="s1" />);
  await act(async () => {});
}

async function send(text = 'hello') {
  fireEvent.change(screen.getByPlaceholderText('Message Seshat...'), { target: { value: text } });
  await act(async () => {
    fireEvent.click(screen.getByLabelText('Send message'));
  });
}

beforeEach(() => {
  // jsdom has no scrollIntoView; StreamingChat calls it on every message change.
  Element.prototype.scrollIntoView = vi.fn();
  deliverToSocket = null;
  for (const m of [mockGetSession, mockGetSessionConfig, mockGetConfig, mockSetSessionEffort, mockSendChatMessage]) {
    m.mockReset();
  }
  mockSendChatMessage.mockResolvedValue(undefined);
  mockGetSessionMessages.mockResolvedValue([]);
  localStorage.clear();
});

afterEach(cleanup);

describe('AC-1 — the selector shows the level of the server', () => {
  it('shows the level from the session GET', async () => {
    mockExistingSession('deep');
    await renderChat();
    expect(effortButton()).toHaveTextContent('Deep');
  });

  it('shows the level of a STATE_DELTA that arrives on the socket (reconnect replay)', async () => {
    mockExistingSession('deep');
    mockGetSessionMessages.mockResolvedValue([{ role: 'user', content: 'hi', timestamp: '2026-10-20T10:00:00+00:00' }]);
    await renderChat(); // history present → the page attaches its socket
    expect(deliverToSocket).not.toBeNull();

    await act(async () => {
      deliverToSocket!({
        type: 'STATE_DELTA',
        seq: null,
        data: { key: 'session_effort', value: { seshat_effort: 'fast', effort_provenance: 'server-hydrated' } },
      });
    });

    expect(effortButton()).toHaveTextContent('Fast');
  });
});

describe('AC-3 — the mode of each level, from the config response', () => {
  it('shows the fast mode for Fast and "model default" for Deep', async () => {
    mockExistingSession('standard');
    await renderChat();

    fireEvent.click(effortButton());
    const list = screen.getByRole('listbox');

    expect(within(list).getByRole('option', { name: /Fast/ })).toHaveTextContent('fast');
    expect(within(list).getByRole('option', { name: /Deep/ })).toHaveTextContent('model default');
  });

  it('shows the mode of a new session from the sessionless config', async () => {
    mockNewSession();
    await renderChat();

    fireEvent.click(effortButton());

    expect(within(screen.getByRole('listbox')).getByRole('option', { name: /Fast/ })).toHaveTextContent('fast');
  });
});

describe('AC-4 — every chat request carries the level that the selector shows', () => {
  it('a new session shows Standard, and its first request carries standard', async () => {
    mockNewSession();
    await renderChat();
    expect(effortButton()).toHaveTextContent('Standard');

    await send();

    expect(mockSendChatMessage).toHaveBeenCalledTimes(1);
    expect(mockSendChatMessage).toHaveBeenCalledWith(expect.objectContaining({ seshatEffort: 'standard' }));
  });

  it('an existing session at Deep sends deep', async () => {
    mockExistingSession('deep');
    await renderChat();

    await send();

    expect(mockSendChatMessage).toHaveBeenCalledWith(expect.objectContaining({ seshatEffort: 'deep' }));
  });

  it('holds Send until the session GET answers, so a Deep session never sends standard', async () => {
    let answer: (v: unknown) => void = () => {};
    mockGetSession.mockReturnValue(new Promise((r) => (answer = r)));
    mockGetSessionConfig.mockResolvedValue(null);
    mockGetConfig.mockResolvedValue(CONFIG);
    await renderChat();
    fireEvent.change(screen.getByPlaceholderText('Message Seshat...'), { target: { value: 'hello' } });
    expect(screen.getByLabelText('Send message')).toBeDisabled();

    await act(async () => answer({ session_id: 's1', seshat_effort: 'deep' }));
    expect(screen.getByLabelText('Send message')).toBeEnabled();
    await act(async () => {
      fireEvent.click(screen.getByLabelText('Send message'));
    });

    expect(mockSendChatMessage).toHaveBeenCalledWith(expect.objectContaining({ seshatEffort: 'deep' }));
  });

  it('a level chosen on a new session (PATCH 404) rides the first request', async () => {
    mockNewSession();
    mockSetSessionEffort.mockRejectedValue(new Error('setSessionEffort failed: 404'));
    await renderChat();

    fireEvent.click(effortButton());
    await act(async () => {
      fireEvent.click(within(screen.getByRole('listbox')).getByRole('option', { name: /Fast/ }));
    });
    expect(effortButton()).toHaveTextContent('Fast');
    await send();

    expect(mockSendChatMessage).toHaveBeenCalledWith(expect.objectContaining({ seshatEffort: 'fast' }));
  });
});

describe('AC-2 — the write and the revert, in the chat screen', () => {
  it('sends one PATCH for a change', async () => {
    mockExistingSession('standard');
    mockSetSessionEffort.mockResolvedValue(undefined);
    await renderChat();

    fireEvent.click(effortButton());
    await act(async () => {
      fireEvent.click(within(screen.getByRole('listbox')).getByRole('option', { name: /Deep/ }));
    });

    expect(mockSetSessionEffort).toHaveBeenCalledTimes(1);
    expect(mockSetSessionEffort).toHaveBeenCalledWith('s1', 'deep');
    expect(effortButton()).toHaveTextContent('Deep');
  });

  it('returns to the stored level and shows the error when the PATCH fails', async () => {
    mockExistingSession('standard');
    mockSetSessionEffort.mockRejectedValue(new Error('setSessionEffort failed: 500'));
    await renderChat();

    fireEvent.click(effortButton());
    await act(async () => {
      fireEvent.click(within(screen.getByRole('listbox')).getByRole('option', { name: /Deep/ }));
    });

    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('500'));
    expect(effortButton()).toHaveTextContent('Standard');
    await send();
    expect(mockSendChatMessage).toHaveBeenCalledWith(expect.objectContaining({ seshatEffort: 'standard' }));
  });
});
