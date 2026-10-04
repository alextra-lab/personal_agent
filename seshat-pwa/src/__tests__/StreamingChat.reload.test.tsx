/**
 * FRE-1543: the call history and the status bar survive an iPadOS page reload.
 *
 * iPadOS reloads the page when the owner switches apps, so the page holds nothing in memory
 * and (in these tests) nothing in localStorage. The server stores the call history on the
 * assistant message and answers an attaching socket with a status snapshot and a replay of
 * the turn in flight. These tests render the real StreamingChat and read what the owner sees.
 */

import { render, screen, act, cleanup, fireEvent, within } from '@testing-library/react';
import { vi, describe, it, expect, beforeEach, afterEach } from 'vitest';
import type { Mock } from 'vitest';

interface MockConnection {
  onEvent: (event: unknown) => void;
  attach: boolean;
}
let connections: Record<string, MockConnection> = {};

vi.mock('next/navigation', () => ({ useRouter: () => ({ push: vi.fn() }) }));
vi.mock('next/link', () => ({
  default: ({ children, href }: { children: React.ReactNode; href: string }) => (
    <a href={href}>{children}</a>
  ),
}));

vi.mock('@/lib/agui-client', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/agui-client')>()),
  connectWebSocket: vi.fn(
    (sessionId: string, onEvent: (e: unknown) => void, _onError?: unknown, opts?: { attach?: boolean }) => {
      connections[sessionId] = { onEvent, attach: opts?.attach === true };
      return { send: vi.fn(), close: vi.fn() };
    },
  ),
  sendChatMessage: vi.fn().mockResolvedValue(undefined),
  getSessionMessages: vi.fn().mockResolvedValue([]),
  getSession: vi.fn().mockResolvedValue({ turn_count: 1, cost_usd: 0.09 }),
  listSessions: vi.fn().mockResolvedValue([]),
  setSessionSelection: vi.fn().mockResolvedValue(undefined),
}));

vi.mock('@/lib/submitTurnRating', () => ({
  submitTurnRating: vi.fn().mockResolvedValue(true),
}));

vi.mock('@/hooks/useSessionConfig', () => ({
  useSessionConfig: () => ({ roles: {}, hydrated: true, refetch: vi.fn() }),
}));

vi.mock('@/components/ChatInput', () => ({
  ChatInput: ({
    onSend,
    isStreaming,
  }: {
    onSend: (text: string, attachments: unknown[]) => void;
    isStreaming: boolean;
  }) => (
    <button data-testid="composer" data-streaming={String(isStreaming)} onClick={() => onSend('hello', [])}>
      send
    </button>
  ),
}));

import { StreamingChat } from '@/components/StreamingChat';
import { getSessionMessages } from '@/lib/agui-client';

const T0 = '2026-10-04T10:00:00+00:00';
const T1 = '2026-10-04T10:00:02.500000+00:00';
const T2 = '2026-10-04T10:00:03+00:00';
const T3 = '2026-10-04T10:00:07+00:00';

let seq = 100;

const phaseStart = (phaseId: string, phase: string, startedAt: string) => ({
  type: 'PHASE_START',
  seq: ++seq,
  data: { phase, phase_id: phaseId, started_at: startedAt, detail: null, parent_id: null },
});

const phaseEnd = (phaseId: string, endedAt: string) => ({
  type: 'PHASE_END',
  seq: ++seq,
  data: { phase_id: phaseId, ok: true, ended_at: endedAt },
});

function turnStatus(tools: [number, number] | null, ctx = 19000, withSeq = true) {
  return {
    type: 'STATE_DELTA',
    seq: withSeq ? ++seq : null,
    data: {
      key: 'turn_status',
      value: {
        context_tokens: ctx,
        context_max: 128000,
        tool_iteration: tools ? tools[0] : null,
        tool_iteration_max: tools ? tools[1] : null,
        turn_cost_usd: 0.09,
        session_cost_usd: 0.09,
        session_context_tokens: ctx,
        compaction_count: 0,
        cache_reset_count: 0,
        quality_alert_count: 0,
        quality_alert: null,
        trace_id: 'trace-1',
      },
    },
  };
}

/** What the server stores on the assistant message for the two-round turn below. */
const STORED_SUMMARY = {
  phases: [
    { phase_id: 'p1', phase: 'planning', detail: null, duration_ms: 2500, state: 'completed', parent_id: null },
    { phase_id: 'p2', phase: 'synthesis', detail: null, duration_ms: 4000, state: 'completed', parent_id: null },
  ],
  tools: [],
  terminal_state: 'completed',
};

const USER_MSG = { role: 'user', content: 'hello', timestamp: T0, trace_id: 'trace-1' };
const ASSISTANT_MSG = {
  role: 'assistant',
  content: 'the answer',
  timestamp: T3,
  trace_id: 'trace-1',
  turn_summary: STORED_SUMMARY,
};

function deliver(sessionId: string, event: unknown) {
  act(() => {
    connections[sessionId].onEvent(event);
  });
}

async function settle() {
  await act(async () => {});
}

/** iPadOS reload: no component state, no localStorage, no sockets. */
function reloadPage(view: { unmount: () => void }) {
  view.unmount();
  localStorage.clear();
  connections = {};
}

const TOOLS_2_OF_25 = /tools 2\/25/;
const CTX_19K = /19K\/128K 15%/;

beforeEach(() => {
  Element.prototype.scrollIntoView = vi.fn();
  connections = {};
  seq = 100;
  localStorage.clear();
  (getSessionMessages as Mock).mockResolvedValue([]);
});

afterEach(() => {
  cleanup();
});

describe('StreamingChat across an iPadOS reload (FRE-1543)', () => {
  it('AC-1/AC-2: after a reload the call history renders the same panel as the live turn, and the snapshot restores the tools count', async () => {
    const live = render(<StreamingChat sessionId="sess-a" />);
    await act(async () => {
      fireEvent.click(screen.getByTestId('composer'));
    });
    deliver('sess-a', phaseStart('p1', 'planning', T0));
    deliver('sess-a', phaseEnd('p1', T1));
    deliver('sess-a', turnStatus([1, 25]));
    deliver('sess-a', phaseStart('p2', 'synthesis', T2));
    deliver('sess-a', phaseEnd('p2', T3));
    deliver('sess-a', turnStatus([2, 25]));
    deliver('sess-a', { type: 'TEXT_DELTA', seq: ++seq, data: { text: 'the answer' } });
    deliver('sess-a', { type: 'DONE', seq: null, trace_id: 'trace-1' });

    const liveHeader = screen.getByTestId('turn-summary-header').textContent;
    const liveRows = within(screen.getByTestId('turn-summary'))
      .getAllByTestId(/^turn-summary-phase-/)
      .map((row) => row.textContent);
    expect(liveHeader).toBe('Completed · 2 phases · 6s');

    reloadPage(live);
    (getSessionMessages as Mock).mockResolvedValue([USER_MSG, ASSISTANT_MSG]);
    render(<StreamingChat sessionId="sess-a" />);
    await settle();

    // The panel comes from the stored message, before any socket frame.
    expect(screen.getByTestId('turn-summary-header').textContent).toBe(liveHeader);
    expect(
      within(screen.getByTestId('turn-summary'))
        .getAllByTestId(/^turn-summary-phase-/)
        .map((row) => row.textContent),
    ).toEqual(liveRows);

    // The page attached; the server's snapshot fills the bar.
    expect(connections['sess-a'].attach).toBe(true);
    deliver('sess-a', turnStatus([2, 25], 19000, false));
    deliver('sess-a', { type: 'REPLAY_COMPLETE', seq: null, last_seq: seq, turn_in_flight: false });
    expect(screen.getByText(TOOLS_2_OF_25)).toBeInTheDocument();
    expect(screen.getByText(CTX_19K)).toBeInTheDocument();
    expect(screen.getByTestId('composer').dataset.streaming).toBe('false');
  });

  it('AC-3: a fresh page with empty localStorage shows the snapshot tools count, ceiling and ctx', async () => {
    (getSessionMessages as Mock).mockResolvedValue([USER_MSG, ASSISTANT_MSG]);
    render(<StreamingChat sessionId="sess-a" />);
    await settle();
    expect(screen.getByText(/tools —\/—/)).toBeInTheDocument();

    deliver('sess-a', turnStatus([2, 25], 19000, false));

    expect(screen.getByText(TOOLS_2_OF_25)).toBeInTheDocument();
    expect(screen.getByText(CTX_19K)).toBeInTheDocument();
  });

  it('AC-4: a reload mid-turn shows the completed round from the replay, then the next live round', async () => {
    (getSessionMessages as Mock).mockResolvedValue([USER_MSG]);
    render(<StreamingChat sessionId="sess-a" />);
    await settle();

    deliver('sess-a', phaseStart('p1', 'planning', T0));
    deliver('sess-a', phaseEnd('p1', T1));
    deliver('sess-a', turnStatus([1, 25]));
    deliver('sess-a', { type: 'REPLAY_COMPLETE', seq: null, last_seq: seq, turn_in_flight: true });

    expect(screen.getByTestId('phase-p1').dataset.state).toBe('completed');
    expect(screen.getByTestId('composer').dataset.streaming).toBe('true');

    deliver('sess-a', phaseStart('p2', 'synthesis', T2));
    expect(screen.getByTestId('phase-p1')).toBeInTheDocument();
    expect(screen.getByTestId('phase-p2').dataset.state).toBe('running');

    deliver('sess-a', phaseEnd('p2', T3));
    deliver('sess-a', { type: 'TEXT_DELTA', seq: ++seq, data: { text: 'the answer' } });
    deliver('sess-a', { type: 'DONE', seq: null, trace_id: 'trace-1' });
    // The replayed round keeps its server duration: 2.5s + 4.0s from server stamps, not the page's clock.
    expect(screen.getByTestId('turn-summary-header').textContent).toBe('Completed · 2 phases · 6s');
  });

  it('a turn that ended between the history fetch and the attach is fetched again, not lost', async () => {
    (getSessionMessages as Mock).mockResolvedValueOnce([USER_MSG]).mockResolvedValue([USER_MSG, ASSISTANT_MSG]);
    render(<StreamingChat sessionId="sess-a" />);
    await settle();
    expect(screen.queryByText('the answer')).not.toBeInTheDocument();

    deliver('sess-a', { type: 'REPLAY_COMPLETE', seq: null, last_seq: 50, turn_in_flight: false });
    await settle();

    expect(screen.getByText('the answer')).toBeInTheDocument();
    expect(screen.getByTestId('turn-summary')).toBeInTheDocument();
  });

  it('AC-5: a snapshot for session A that lands after a switch to B never paints on B', async () => {
    (getSessionMessages as Mock).mockResolvedValue([USER_MSG, ASSISTANT_MSG]);
    const view = render(<StreamingChat sessionId="sess-a" />);
    await settle();
    const socketA = connections['sess-a'];

    (getSessionMessages as Mock).mockResolvedValue([]);
    view.rerender(<StreamingChat sessionId="sess-b" />);
    await settle();
    act(() => socketA.onEvent(turnStatus([2, 25], 19000, false)));

    expect(screen.getByText(/tools —\/—/)).toBeInTheDocument();
    expect(screen.queryByText(CTX_19K)).not.toBeInTheDocument();
  });
});

describe('FRE-1543 review: only an attach can trigger the history refetch', () => {
  it("an ordinary reconnect's REPLAY_COMPLETE just after a send never replaces the transcript", async () => {
    render(<StreamingChat sessionId="sess-a" />);
    await settle();
    await act(async () => {
      fireEvent.click(screen.getByTestId('composer'));
    });
    (getSessionMessages as Mock).mockClear();

    // A send's socket reconnects; the server has not stored the user message yet.
    deliver('sess-a', { type: 'REPLAY_COMPLETE', seq: null });
    await settle();

    expect(getSessionMessages).not.toHaveBeenCalled();
    expect(screen.getByText('hello')).toBeInTheDocument();
  });
});
