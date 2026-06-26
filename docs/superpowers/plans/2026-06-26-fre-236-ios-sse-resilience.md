# FRE-236: PWA iOS Background / Multitask Protection — Phase 1

**Date:** 2026-06-26  
**Branch:** `fre-236-ios-sse-resilience`  
**Tier:** Sonnet (T2)

## Scope (Phase 1 only — no backend changes)

- `seshat-pwa/src/lib/agui-client.ts` — add `onWsConnected`/`onWsDisconnected` callbacks; emit `hidden_duration_ms` in WS CONNECT payload for telemetry
- `seshat-pwa/src/hooks/useSSEStream.ts` — `isReconnecting` state, draft persistence, clear on DONE
- `seshat-pwa/src/components/StreamingChat.tsx` — "Reconnecting…" banner
- `seshat-pwa/src/__tests__/agui-client.visibility.test.ts` (new) — WS callback unit tests
- `seshat-pwa/src/__tests__/useSSEStream.test.tsx` — extend with `isReconnecting` + draft tests

Phase 2 (background sync, resume-from-offset, Web Push) is out of scope.

---

## Background — what already works

- `connectWebSocket` already handles WS reconnect with exponential backoff.
- `visibilitychange → visible` already triggers reconnect if WS is closed.
- The seq/REPLAY_GAP path already reconciles mid-turn state via `getSessionMessages`.
- On kill + relaunch, `StreamingChat`'s `sessionId` effect already calls `getSessionMessages`.

## What is missing (the actual gap)

1. No UI feedback when WS drops mid-turn — user sees frozen `…` dots with no explanation.
2. No draft persistence to localStorage — nothing marks "a turn was in-flight when hidden" for relaunch context.
3. `persistSeqOnHide()` in `agui-client.ts` is a no-op comment.
4. No `visibilitychange` telemetry.

---

## Steps

### Step 1 — Write failing tests (TDD)

**File: `seshat-pwa/src/__tests__/agui-client.visibility.test.ts`** (new)

Test suite covering `connectWebSocket` new callbacks:

```
describe('connectWebSocket — onWsConnected / onWsDisconnected callbacks')
  it('calls onWsConnected when WS opens')
  it('calls onWsDisconnected on unexpected close (not superseded, not intentional)')
  it('does NOT call onWsDisconnected when close() is called intentionally')
  it('does NOT call onWsDisconnected on superseded close (code 4001)')
  it('includes hidden_duration_ms in CONNECT payload after visibility hide → visible cycle')
```

Test command: `cd seshat-pwa && npx vitest run src/__tests__/agui-client.visibility.test.ts`
Expected: all FAIL (callbacks not implemented yet).

**File: `seshat-pwa/src/__tests__/useSSEStream.test.tsx`** — extend existing

Add to existing mock of `connectWebSocket` to capture `opts` (4th param):

```ts
let capturedOpts: { onWsConnected?: () => void; onWsDisconnected?: () => void } | null = null;

vi.mock('@/lib/agui-client', () => ({
  ...existing...
  connectWebSocket: vi.fn((_sid, onEvent, _onErr, opts) => {
    capturedOnEvent = onEvent;
    capturedOpts = opts ?? null;
    ...
  }),
}));
```

New test cases:
```
describe('useSSEStream — isReconnecting')
  it('is false initially')
  it('is set true when onWsDisconnected fires while isStreaming')
  it('stays false when onWsDisconnected fires while NOT streaming')
  it('is cleared to false by DONE event')
  it('is cleared to false by sendMessage')

describe('useSSEStream — draft persistence on hide')
  it('writes draft to localStorage on visibilitychange→hidden while streaming')
  it('does not write draft when not streaming')
  it('clears draft from localStorage on DONE event')
  it('clears draft from localStorage on sendMessage')
```

Test command: `cd seshat-pwa && npx vitest run src/__tests__/useSSEStream.test.tsx`
Expected: new cases FAIL.

---

### Step 2 — Extend `connectWebSocket` in `agui-client.ts`

**File: `seshat-pwa/src/lib/agui-client.ts`**

Add 4th parameter `opts` to `connectWebSocket`:

```ts
interface ConnectWebSocketOpts {
  onWsConnected?: () => void;
  onWsDisconnected?: () => void;
}

export function connectWebSocket(
  sessionId: string,
  onEvent: AGUIEventHandler,
  onError?: ErrorHandler,
  opts?: ConnectWebSocketOpts,
): StreamConnection
```

Changes inside `connectWebSocket`:

1. **`ws.onopen`**: after sending CONNECT handshake, call `opts?.onWsConnected?.()`. Also compute and include `hidden_duration_ms` in the CONNECT payload when returning from a visibility-hide cycle:

```ts
// Track when WS was last hidden (for telemetry)
let hiddenAt: number | null = null;

// in handleVisibilityChange:
if (document.visibilityState === 'hidden') {
  if (ws?.readyState === WebSocket.OPEN) {
    hiddenAt = Date.now();
  }
}

// in ws.onopen, before the CONNECT send:
const connectPayload: Record<string, unknown> = { type: 'CONNECT', last_seq: lastSeq };
if (hiddenAt !== null) {
  connectPayload['hidden_duration_ms'] = Date.now() - hiddenAt;
  hiddenAt = null;
}
ws?.send(JSON.stringify(connectPayload));

// after send:
opts?.onWsConnected?.();
```

2. **`ws.onclose`**: after `scheduleReconnect()`, if NOT intentional close AND NOT superseded, call `opts?.onWsDisconnected?.()`:

```ts
ws.onclose = (ev: CloseEvent) => {
  cleanup();
  if (generation !== connectGeneration) return;
  if (closed || ev.code === WS_CLOSE_SUPERSEDED) return;
  opts?.onWsDisconnected?.();
  scheduleReconnect();
};
```

3. **`persistSeqOnHide`**: keep as-is (seq already persisted per-event; the function now just tracks `hiddenAt`). Actually fold tracking into `handleVisibilityChange` directly (the function was always a no-op).

4. **`close()`** method: set `closed = true` BEFORE the `onclose` fires (already is via `ws.onclose = null` then `ws.close()`), so `onWsDisconnected` never fires on intentional close. Verify current code already does this:

```ts
close: () => {
  closed = true;   // <-- guards onclose
  ...
  ws.onclose = null;  // <-- prevents onclose from running
  ws.close();
```

Current code already nulls `ws.onclose` before closing — so `onWsDisconnected` won't fire. ✓

---

### Step 3 — Add `isReconnecting` + draft persistence to `useSSEStream.ts`

**File: `seshat-pwa/src/hooks/useSSEStream.ts`**

**3a. Add constant and ref:**

```ts
const DRAFT_KEY = (sid: string) => `seshat_bg_draft_${sid}`;
```

Add after existing refs:
```ts
const isStreamingRef = useRef(false);
```

**3b. Add `isReconnecting` state:**

```ts
const [isReconnecting, setIsReconnecting] = useState(false);
```

**3c. Sync `isStreamingRef` alongside `setIsStreaming` calls:**

Wherever `setIsStreaming(true/false)` is called, also set `isStreamingRef.current`:
- `sendMessage` start: `setIsStreaming(true); isStreamingRef.current = true;`
- DONE handler: `setIsStreaming(false); isStreamingRef.current = false;`
- CANCELLED handler: `setIsStreaming(false); isStreamingRef.current = false;`
- RUN_ERROR handler: `setIsStreaming(false); isStreamingRef.current = false;`
- INTERRUPT handler: `setIsStreaming(false); isStreamingRef.current = false;`
- `disconnect()`: `setIsStreaming(false); isStreamingRef.current = false;`

**3d. Add `visibilitychange` + `pagehide` listener (runs once on mount):**

```ts
useEffect(() => {
  const persistDraft = () => {
    const sid = currentSessionRef.current;
    const content = currentContentRef.current;
    if (isStreamingRef.current && sid && content) {
      try {
        localStorage.setItem(DRAFT_KEY(sid), JSON.stringify({
          content,
          at: new Date().toISOString(),
        }));
      } catch {
        // quota exceeded — skip
      }
    }
  };
  const onVisChange = () => {
    if (document.visibilityState === 'hidden') persistDraft();
  };
  if (typeof document !== 'undefined') {
    document.addEventListener('visibilitychange', onVisChange);
    window.addEventListener('pagehide', persistDraft);
  }
  return () => {
    if (typeof document !== 'undefined') {
      document.removeEventListener('visibilitychange', onVisChange);
      window.removeEventListener('pagehide', persistDraft);
    }
  };
}, []); // deps: none — reads from stable refs
```

**3e. Pass `opts` to `connectWebSocket` in `sendMessage`:**

```ts
streamRef.current = connectWebSocket(
  sessionId,
  handleEvent,
  () => { /* WS error */ },
  {
    onWsDisconnected: () => {
      if (isStreamingRef.current) {
        setIsReconnecting(true);
      }
    },
    onWsConnected: () => {
      setIsReconnecting(false);
    },
  },
);
```

Note: `onWsConnected` fires on EVERY successful WS open (initial + reconnect). Clearing `isReconnecting` here is safe — the DONE event will arrive next and finalize state.

**3f. In DONE handler** (already clears tool state — add draft/reconnect clearing):

```ts
case 'DONE': {
  setIsStreaming(false);
  isStreamingRef.current = false;
  setIsReconnecting(false);
  if (currentSessionRef.current) {
    localStorage.removeItem(DRAFT_KEY(currentSessionRef.current));
  }
  // ... existing DONE logic (trace stamp, tool state persistence) ...
}
```

**3g. In `sendMessage`** (before connecting WS, clear draft for this session):

```ts
const sendMessage = useCallback(async (text, sessionId, profile) => {
  // Close existing stream
  streamRef.current?.close();
  streamRef.current = null;
  
  // Clear any stale draft for this session
  if (typeof localStorage !== 'undefined') {
    localStorage.removeItem(DRAFT_KEY(sessionId));
  }
  setIsReconnecting(false);
  // ... rest of existing sendMessage ...
}, [handleEvent]);
```

**3h. Expose `isReconnecting` in return type:**

Add to `UseSSEStreamReturn` interface:
```ts
/** True when WS was lost mid-turn and we are waiting to reconnect. */
isReconnecting: boolean;
```

Add to return object.

---

### Step 4 — "Reconnecting…" banner in `StreamingChat.tsx`

**File: `seshat-pwa/src/components/StreamingChat.tsx`**

Destructure from hook:
```ts
const {
  ...existing fields...,
  isReconnecting,
} = useSSEStream();
```

In the message list `<main>` body, add banner as the first child (sticky top):

```tsx
{isReconnecting && (
  <div className="sticky top-0 z-10 px-4 py-2 bg-amber-900/80 backdrop-blur-sm text-amber-200 text-xs text-center border-b border-amber-800/50">
    Reconnecting…
  </div>
)}
```

---

### Step 5 — Run all tests to verify

```bash
cd seshat-pwa && npx vitest run src/__tests__/agui-client.visibility.test.ts
cd seshat-pwa && npx vitest run src/__tests__/useSSEStream.test.tsx
cd seshat-pwa && npx vitest run
```

Expected: all pass.

Then quality gates (from repo root):
```bash
make mypy          # Python only — should be clean
make ruff-check
make ruff-format
make test          # Python unit tests
cd seshat-pwa && npm run build
```

---

## Acceptance criteria mapping

| AC | Satisfied by |
|----|-------------|
| Frozen `…` / truncated message → UI shows reconnecting/failed | `isReconnecting` banner + REPLAY_GAP reconcile path |
| Kill + relaunch → authoritative state from server | Existing `getSessionMessages` on mount (unchanged) + draft key signals "was in-flight" for future Phase 2 resume |
| `visibilitychange` transitions logged in telemetry | `hidden_duration_ms` in WS CONNECT payload → backend log |
| Works on iPhone Safari PWA | Verification: manual iPhone background + foreground cycling |
| No regression desktop | Full vitest suite passes |

## Out of scope (Phase 2 tickets to file)

- SW `sync` event handler for queued outbound messages
- Backend resume-from-offset on `/stream/{session_id}`
- Web Push / APNs notification when long turn completes while backgrounded
