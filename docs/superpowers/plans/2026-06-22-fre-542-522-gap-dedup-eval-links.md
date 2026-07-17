# FRE-542 + FRE-522 — Gap-aware client dedup & eval-session cross-links

**Date**: 2026-06-22  
**Tickets**: FRE-542 (Tier-2:Sonnet) · FRE-522 (Tier-2:Sonnet)  
**Refs**: ADR-0075 (WS transport), FRE-518 (server-side fix)

---

## FRE-542 — Gap-aware PWA client dedup

### Problem

`agui-client.ts:277-280` does max-seq dedup:

```ts
if (parsed.seq <= getLastSeq()) return;  // drop
setLastSeq(parsed.seq);                  // advance to max
```

If server delivers seq=2 before seq=1 (the exact FRE-518 failure mode), lastSeq
advances to 2 and seq=1 is permanently dropped — both live and on reconnect replay
(reconnect sends `last_seq: 2`, server skips seq=1 forever).

### Fix

Track two watermarks:
- `ackSeq` (contiguous watermark, persisted) — highest seq where all prior seqs
  have been received and dispatched. Used for reconnect `last_seq`.
- `pendingBuf: Map<number, AGUIEvent>` (in-memory) — out-of-order events buffered
  until their gap fills.

On reconnect, clear `pendingBuf` and reset `lastSeen` to `ackSeq`.

### Implementation steps

**Step A — Failing test** (`seshat-pwa/src/__tests__/agui-client.gap-dedup.test.ts`)

Mock `global.WebSocket` with a test double that exposes `triggerOpen()` and
`triggerMessage(data)`. Call `connectWebSocket`. Assert:

1. Out-of-order: inject seq=2, then seq=1 → `onEvent` called with seq=1, then seq=2
   (correct order, no events dropped)
2. In-order: inject seq=1, seq=2 → both dispatched (no regression)
3. Reconnect watermark: after seq=1 (ackSeq=1) with pending seq=3 buffered,
   CONNECT message on reconnect must carry `last_seq: 1` (not 3)

Run: `cd seshat-pwa && npx vitest run src/__tests__/agui-client.gap-dedup.test.ts`
Expect: all 3 tests FAIL before implementation.

**Step B — Implementation** (`seshat-pwa/src/lib/agui-client.ts`)

Inside `connectWebSocket`:

1. Replace `seqKey` → `ackSeqKey = \`seshat_ack_seq_${sessionId}\``
2. Replace `getLastSeq()`/`setLastSeq()` → `getAckSeq()`/`setAckSeq()`
3. Add `let lastSeen = 0` and `const pendingBuf = new Map<number, AGUIEvent>()`
4. At top of `connect()` (before creating WebSocket): `lastSeen = getAckSeq(); pendingBuf.clear()`
5. In `ws.onopen`: use `getAckSeq()` (not lastSeen) for `last_seq`
6. Replace `ws.onmessage` dedup logic:

```ts
if (parsed.seq != null) {
  const seq = parsed.seq;
  const ackSeq = getAckSeq();
  if (seq <= ackSeq || pendingBuf.has(seq)) return; // duplicate
  if (seq === ackSeq + 1) {
    onEvent(parsed);
    setAckSeq(seq);
    let next = getAckSeq() + 1;
    while (pendingBuf.has(next)) {
      onEvent(pendingBuf.get(next)!);
      pendingBuf.delete(next);
      setAckSeq(next);
      next = getAckSeq() + 1;
    }
  } else {
    pendingBuf.set(seq, parsed);
  }
  return;
}
```

**Step C — Verify**
```bash
cd seshat-pwa && npx vitest run src/__tests__/agui-client.gap-dedup.test.ts
cd seshat-pwa && npx vitest run
```

**Quality gates**:
```bash
cd seshat-pwa && npx tsc --noEmit
```

---

## FRE-522 — Eval-session cross-links

### Problem

Eval reports (`telemetry/evaluation/*/`) carry `session_id` + `trace_id` per case but
render them as code snippets, not links. You can't navigate from report → PWA session.

The PWA session list also doesn't distinguish eval sessions (channel=EVAL) from regular ones.

Tool-use not shown in eval sessions: FAITHFUL — `TOOL_CALL_START`/`END` are transient
WS events not stored in message history. Historical sessions only have user+assistant
messages. No rendering fix needed; add a note in the harness.

### Implementation steps

**Step A — PWA deep links in harness** (`scripts/eval/fre453_canonical_evalset/harness.py`)

1. Add `--pwa-url` CLI arg (default `https://seshat.example.com`)
2. Store in `run_meta` as `pwa_url`
3. Pass `pwa_url: str` param to `render_markdown()`
4. Update line 756 in `render_markdown`:

```python
lines.append(
    f"- trace_id: `{row.trace_id}`  ·  "
    f"session: [{row.session_id}]({pwa_url}/c/{row.session_id})"
)
```

5. Add note about tool-use in comments near `call_chat` (tools are transient WS events).

**Step B — Update harness tests** (`tests/evaluation/test_fre453_canonical_evalset.py`)

In `TestRenderer.test_markdown_contains_core_sections`: pass `pwa_url` kwarg and
assert the markdown contains the PWA link pattern `(/c/` substring).

**Step C — EVAL badge in SessionList** (`seshat-pwa/src/components/SessionList.tsx`)

In the per-session `<li>` block, add a small pill after the session title when
`s.channel === 'EVAL'`:

```tsx
{s.channel === 'EVAL' && (
  <span className="ml-1.5 text-[10px] font-mono text-sky-400/70 border border-sky-400/30 rounded px-1">
    EVAL
  </span>
)}
```

This requires `SessionSummary.channel` (already typed as `string | null`).

**Step D — Run tests**
```bash
make test-k test_fre453_canonical_evalset
cd seshat-pwa && npx vitest run
```

---

## Branch + PR strategy

Two separate PRs (one ticket = one PR per lifecycle-rules):
- **PR-A**: `fre-542-gap-aware-dedup` — agui-client.ts + gap-dedup test
- **PR-B**: `fre-522-eval-session-links` — harness.py + SessionList.tsx + harness test update

Both PRs target `main`. Ship PR-A first (pure PWA/TS), PR-B second (spans Python + TS).

---

## Acceptance criteria checklist

### FRE-542
- [ ] Out-of-order delivery (seq=2 → seq=1): seq=1 not dropped, both dispatched in order
- [ ] Normal in-order path: no regression
- [ ] Reconnect sends ackSeq (contiguous watermark), not max-seen
- [ ] Unit test covers all three scenarios

### FRE-522
- [ ] eval markdown report renders session_id as clickable PWA link
- [ ] `--pwa-url` arg lets operator customize the PWA base URL
- [ ] TestRenderer covers the link
- [ ] SessionList shows EVAL badge for `channel === 'EVAL'` sessions
- [ ] Tool-use rendering gap documented as faithful (not a bug)
