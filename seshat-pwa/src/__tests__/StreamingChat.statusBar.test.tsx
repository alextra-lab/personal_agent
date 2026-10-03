/**
 * FRE-1538: the status bar keeps its values.
 *
 * Owner decisions (2026-10-03):
 *  - tools lane: keeps the last turn's count until the next send, then resets;
 *  - ctx lane: never resets at a send, reconnect, remount or reload — only a new
 *    reading (a compaction reading included) replaces it; a new session is —/—.
 *
 * These tests render the real StreamingChat and read the real TurnStatusBar text,
 * so they assert what the owner sees, not the wiring. ChatInput is a stub that
 * exposes a send button; the network layer is mocked per session.
 */

import { render, screen, act, cleanup, fireEvent } from '@testing-library/react';
import { vi, describe, it, expect, beforeEach, afterEach } from 'vitest';
import type { Mock } from 'vitest';

interface MockConnection {
  onEvent: (event: unknown) => void;
  callbacks: { onWsDisconnected?: () => void; onWsConnected?: () => void };
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
    (
      sessionId: string,
      onEvent: (e: unknown) => void,
      _onError?: unknown,
      callbacks: MockConnection['callbacks'] = {},
    ) => {
    connections[sessionId] = { onEvent, callbacks };
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
  ChatInput: ({ onSend }: { onSend: (text: string, attachments: unknown[]) => void }) => (
    <button onClick={() => onSend('hello', [])}>send</button>
  ),
}));

import { StreamingChat } from '@/components/StreamingChat';
import { getSession } from '@/lib/agui-client';

let seq = 0;

/** A turn_status STATE_DELTA. `ctx`/`max` null models "ceiling not yet resolved". */
function turnStatusEvent(opts: {
  ctx: number;
  max: number | null;
  tools?: [number, number] | null;
}) {
  return {
    type: 'STATE_DELTA',
    seq: ++seq,
    data: {
      key: 'turn_status',
      value: {
        context_tokens: opts.ctx,
        context_max: opts.max,
        tool_iteration: opts.tools ? opts.tools[0] : null,
        tool_iteration_max: opts.tools ? opts.tools[1] : null,
        turn_cost_usd: 0.09,
        session_cost_usd: 0.09,
        session_context_tokens: opts.ctx,
        compaction_count: 0,
        cache_reset_count: 0,
        quality_alert_count: 0,
        quality_alert: null,
        trace_id: 'trace-1',
      },
    },
  };
}

const doneEvent = () => ({ type: 'DONE', seq: ++seq, data: {} });

function deliver(sessionId: string, event: unknown) {
  act(() => {
    connections[sessionId].onEvent(event);
  });
}

async function send() {
  await act(async () => {
    fireEvent.click(screen.getByText('send'));
  });
}

/** Drive one tool-using turn on `sessionId`: send, three rounds, DONE. */
async function runToolTurn(sessionId: string) {
  await send();
  deliver(sessionId, turnStatusEvent({ ctx: 12000, max: 128000, tools: [1, 6] }));
  deliver(sessionId, turnStatusEvent({ ctx: 12000, max: 128000, tools: [2, 6] }));
  deliver(sessionId, turnStatusEvent({ ctx: 12000, max: 128000, tools: [3, 6] }));
  deliver(sessionId, doneEvent());
}

const CTX_12K = /12K\/128K 9%/;
const TOOLS_3_OF_6 = /tools 3\/6/;
const TOOLS_UNKNOWN = /tools —\/—/;
const CTX_UNKNOWN = /^—\/—$/;

beforeEach(() => {
  // jsdom has no scrollIntoView; StreamingChat calls it on every message change.
  Element.prototype.scrollIntoView = vi.fn();
  connections = {};
  seq = 0;
  localStorage.clear();
  (getSession as Mock).mockResolvedValue({ turn_count: 1, cost_usd: 0.09 });
});

afterEach(() => {
  cleanup();
});

describe('StreamingChat status bar (FRE-1538)', () => {
  it('AC-1/AC-2: after tool rounds, DONE and a remount of the same session, tools and ctx show the last values', async () => {
    const first = render(<StreamingChat sessionId="sess-a" />);
    await runToolTurn('sess-a');
    expect(screen.getByText(TOOLS_3_OF_6)).toBeInTheDocument();
    expect(screen.getByText(CTX_12K)).toBeInTheDocument();

    first.unmount();
    render(<StreamingChat sessionId="sess-a" />);
    // Let the history/session fetches settle — they must not clear the bar either.
    await act(async () => {});

    expect(screen.getByText(TOOLS_3_OF_6)).toBeInTheDocument();
    expect(screen.getByText(CTX_12K)).toBeInTheDocument();
  });

  it('AC-2/AC-3: the next send resets the tools lane and leaves the ctx lane alone', async () => {
    const first = render(<StreamingChat sessionId="sess-a" />);
    await runToolTurn('sess-a');
    first.unmount();
    render(<StreamingChat sessionId="sess-a" />);
    await act(async () => {});

    await send();

    expect(screen.getByText(TOOLS_UNKNOWN)).toBeInTheDocument();
    expect(screen.getByText(CTX_12K)).toBeInTheDocument();

    // The new turn's live count replaces the reset lane.
    deliver('sess-a', turnStatusEvent({ ctx: 13000, max: 128000, tools: [1, 6] }));
    expect(screen.getByText(/tools 1\/6/)).toBeInTheDocument();
  });

  it('AC-3: a reading with no resolved ceiling does not blank ctx; a lower (compaction) reading replaces it', async () => {
    render(<StreamingChat sessionId="sess-a" />);
    await runToolTurn('sess-a');
    await send();

    // Early in the new turn the server has not resolved the ceiling yet.
    deliver('sess-a', turnStatusEvent({ ctx: 0, max: null, tools: null }));
    expect(screen.getByText(CTX_12K)).toBeInTheDocument();

    // Compaction ran: the post-compaction reading is a new reading and wins.
    deliver('sess-a', turnStatusEvent({ ctx: 4000, max: 128000, tools: null }));
    expect(screen.getByText(/4\.0K\/128K 3%/)).toBeInTheDocument();
    expect(screen.queryByText(CTX_12K)).not.toBeInTheDocument();
  });

  it('AC-3: a new session with no reading shows —/— on both lanes, never 0', async () => {
    render(<StreamingChat sessionId="sess-new" />);
    await act(async () => {});

    expect(screen.getByText(TOOLS_UNKNOWN)).toBeInTheDocument();
    expect(screen.getByText(CTX_UNKNOWN)).toBeInTheDocument();
  });

  it('AC-4: a page reload mid-turn (no DONE seen, no in-memory state) restores both lanes from storage', async () => {
    const first = render(<StreamingChat sessionId="sess-a" />);
    await send();
    deliver('sess-a', turnStatusEvent({ ctx: 12000, max: 128000, tools: [3, 6] }));
    // iPadOS drops the page here: unmount without DONE.
    first.unmount();
    connections = {};

    render(<StreamingChat sessionId="sess-a" />);
    await act(async () => {});

    expect(screen.getByText(TOOLS_3_OF_6)).toBeInTheDocument();
    expect(screen.getByText(CTX_12K)).toBeInTheDocument();
  });

  it("AC-5: switching to a session with no values shows —/—, switching back shows A's values, and a late event for A cannot touch B", async () => {
    const view = render(<StreamingChat sessionId="sess-a" />);
    await runToolTurn('sess-a');

    view.rerender(<StreamingChat sessionId="sess-b" />);
    await act(async () => {});
    expect(screen.getByText(TOOLS_UNKNOWN)).toBeInTheDocument();
    expect(screen.getByText(CTX_UNKNOWN)).toBeInTheDocument();
    expect(localStorage.getItem('seshat-turn-status-sess-b')).toBeNull();

    // A straggling frame from A's old socket (FRE-1414 gate) changes nothing on B.
    deliver('sess-a', turnStatusEvent({ ctx: 99000, max: 128000, tools: [5, 6] }));
    expect(screen.getByText(TOOLS_UNKNOWN)).toBeInTheDocument();
    expect(screen.getByText(CTX_UNKNOWN)).toBeInTheDocument();
    expect(localStorage.getItem('seshat-turn-status-sess-b')).toBeNull();

    view.rerender(<StreamingChat sessionId="sess-a" />);
    await act(async () => {});
    expect(screen.getByText(TOOLS_3_OF_6)).toBeInTheDocument();
    expect(screen.getByText(CTX_12K)).toBeInTheDocument();
  });

  it('a socket drop and reconnect (iPadOS suspend/resume) leaves both lanes unchanged', async () => {
    render(<StreamingChat sessionId="sess-a" />);
    await runToolTurn('sess-a');

    act(() => connections['sess-a'].callbacks.onWsDisconnected?.());
    act(() => connections['sess-a'].callbacks.onWsConnected?.());

    expect(screen.getByText(TOOLS_3_OF_6)).toBeInTheDocument();
    expect(screen.getByText(CTX_12K)).toBeInTheDocument();
  });

  it('a late session fetch never clobbers a live reading that arrived first (only cost comes from REST)', async () => {
    let resolveSession: (v: unknown) => void = () => {};
    (getSession as Mock).mockReturnValue(new Promise((r) => (resolveSession = r)));

    render(<StreamingChat sessionId="sess-a" />);
    await send();
    deliver('sess-a', turnStatusEvent({ ctx: 12000, max: 128000, tools: [3, 6] }));

    await act(async () => {
      resolveSession({ turn_count: 1, cost_usd: 0.5 });
    });

    expect(screen.getByText(TOOLS_3_OF_6)).toBeInTheDocument();
    expect(screen.getByText(CTX_12K)).toBeInTheDocument();
    expect(screen.getByText('$0.50')).toBeInTheDocument();
  });

  it('a finite tools reading followed by a null one inside one turn shows no reading, and a reload agrees with the screen', async () => {
    const first = render(<StreamingChat sessionId="sess-a" />);
    await send();
    deliver('sess-a', turnStatusEvent({ ctx: 12000, max: 128000, tools: [3, 6] }));
    deliver('sess-a', turnStatusEvent({ ctx: 12000, max: 128000, tools: null }));
    expect(screen.getByText(TOOLS_UNKNOWN)).toBeInTheDocument();

    first.unmount();
    render(<StreamingChat sessionId="sess-a" />);
    await act(async () => {});

    expect(screen.getByText(TOOLS_UNKNOWN)).toBeInTheDocument();
    expect(screen.getByText(CTX_12K)).toBeInTheDocument();
  });
});
