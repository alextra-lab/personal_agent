import type { TurnStatus } from '@/lib/types';

/**
 * FRE-1538 — the status bar keeps its values.
 *
 * Owner decisions of 2026-10-03:
 *  - tools lane (engagement): keeps the last turn's count until the next send;
 *  - ctx lane (session): never resets at a send, reconnect, remount or reload. Only a new
 *    reading replaces it (a compaction reading is a new reading). A new session is —/—.
 *
 * Storage is per-session localStorage, not a server snapshot: the bar shows the *live*
 * `session_context_tokens`, the REST session endpoint offers only an estimate, and the
 * projector's session lane is process-local (FRE-1401). The record is keyed by session id,
 * so a session can never read another session's numbers.
 *
 * Absent ≠ zero (FRE-928 / FRE-935): a reading that never arrived is never stored, never
 * loaded, and never invented.
 */

/** Storage key for one session's last status-bar readings. */
const storageKey = (sessionId: string) => `seshat-turn-status-${sessionId}`;

/**
 * The cold reading: no context ceiling has resolved and no tool count has arrived, so both
 * meter halves and the tools lane render "—" (FRE-1401).
 */
export const COLD_TURN_STATUS: TurnStatus = {
  context_tokens: 0,
  context_max: null,
  tool_iteration: null,
  tool_iteration_max: null,
  turn_cost_usd: 0,
  session_cost_usd: 0,
  session_context_tokens: 0,
  compaction_count: 0,
  cache_reset_count: 0,
  quality_alert_count: 0,
  quality_alert: null,
};

interface CtxReading {
  context_tokens: number;
  session_context_tokens: number;
  context_max: number;
}

interface ToolsReading {
  tool_iteration: number;
  tool_iteration_max: number;
}

interface StoredRecord {
  ctx?: CtxReading;
  tools?: ToolsReading;
}

const isFiniteNumber = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v);

/**
 * Whether a status carries a real context reading.
 *
 * A reading needs a resolved, positive ceiling and a numeric usage. A `null` ceiling means
 * the server has not resolved it yet — that is not a reading.
 *
 * @param status - The status to inspect.
 * @returns True when the ctx lane holds a genuine reading.
 */
export function hasCtxReading(status: TurnStatus): boolean {
  return (
    isFiniteNumber(status.context_max) &&
    status.context_max > 0 &&
    isFiniteNumber(status.session_context_tokens)
  );
}

function hasToolsReading(status: TurnStatus): boolean {
  return isFiniteNumber(status.tool_iteration) && isFiniteNumber(status.tool_iteration_max);
}

/**
 * Fold a newly arrived status onto the displayed one.
 *
 * The ctx lane carries over from `prev` when `next` holds no ctx reading, so a turn that
 * starts before its ceiling resolves cannot blank the headroom figure. The tools lane
 * always comes from `next`.
 *
 * @param prev - The status currently displayed, or null before any status.
 * @param next - The status that just arrived.
 * @returns The status to display and persist.
 */
export function mergeTurnStatus(prev: TurnStatus | null, next: TurnStatus): TurnStatus {
  if (prev === null || hasCtxReading(next) || !hasCtxReading(prev)) return next;
  return {
    ...next,
    context_tokens: prev.context_tokens,
    session_context_tokens: prev.session_context_tokens,
    context_max: prev.context_max,
  };
}

function readRecord(sessionId: string): StoredRecord {
  try {
    const raw = localStorage.getItem(storageKey(sessionId));
    if (!raw) return {};
    const parsed: unknown = JSON.parse(raw);
    if (parsed === null || typeof parsed !== 'object') return {};
    const { ctx, tools } = parsed as { ctx?: Partial<CtxReading>; tools?: Partial<ToolsReading> };
    const record: StoredRecord = {};
    // Each lane loads as an atomic group: a half-valid group is dropped whole, so a valid
    // ceiling can never pair with the cold numerator 0 and render "0/max".
    if (
      ctx &&
      isFiniteNumber(ctx.context_tokens) &&
      isFiniteNumber(ctx.session_context_tokens) &&
      isFiniteNumber(ctx.context_max) &&
      ctx.context_max > 0
    ) {
      record.ctx = {
        context_tokens: ctx.context_tokens,
        session_context_tokens: ctx.session_context_tokens,
        context_max: ctx.context_max,
      };
    }
    if (tools && isFiniteNumber(tools.tool_iteration) && isFiniteNumber(tools.tool_iteration_max)) {
      record.tools = {
        tool_iteration: tools.tool_iteration,
        tool_iteration_max: tools.tool_iteration_max,
      };
    }
    return record;
  } catch {
    // Corrupt entry or unavailable storage — treat as no record.
    return {};
  }
}

function writeRecord(sessionId: string, record: StoredRecord): void {
  try {
    if (record.ctx === undefined && record.tools === undefined) {
      localStorage.removeItem(storageKey(sessionId));
    } else {
      localStorage.setItem(storageKey(sessionId), JSON.stringify(record));
    }
  } catch {
    // Quota or private mode — the bar still works from memory.
  }
}

/**
 * Load a session's last readings onto the cold status.
 *
 * @param sessionId - The session whose bar is being shown.
 * @returns The cold status with the session's stored ctx and tools readings, if any.
 */
export function loadTurnStatus(sessionId: string): TurnStatus {
  const { ctx, tools } = readRecord(sessionId);
  return { ...COLD_TURN_STATUS, ...ctx, ...tools };
}

/**
 * Persist the displayed status so a remount or reload shows it again.
 *
 * The record is a pure function of `status`: a lane with no reading is left out, so storage
 * and screen cannot disagree. Pass the status already folded by {@link mergeTurnStatus}.
 *
 * @param sessionId - The session the status belongs to (the connection's bound session).
 * @param status - The status being displayed.
 */
export function persistTurnStatus(sessionId: string, status: TurnStatus): void {
  const record: StoredRecord = {};
  if (hasCtxReading(status)) {
    record.ctx = {
      context_tokens: status.context_tokens,
      session_context_tokens: status.session_context_tokens,
      context_max: status.context_max as number,
    };
  }
  if (hasToolsReading(status)) {
    record.tools = {
      tool_iteration: status.tool_iteration as number,
      tool_iteration_max: status.tool_iteration_max as number,
    };
  }
  writeRecord(sessionId, record);
}

/**
 * Drop the stored tools reading, keeping the ctx reading. Called at a send, when the tools
 * lane resets.
 *
 * @param sessionId - The session the user is sending on.
 */
export function clearStoredTools(sessionId: string): void {
  const { ctx } = readRecord(sessionId);
  writeRecord(sessionId, ctx ? { ctx } : {});
}
