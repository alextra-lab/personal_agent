/**
 * Low-level AG-UI client utilities.
 *
 * Provides helpers for interacting with the Seshat backend:
 * - Sending chat messages via POST /chat/stream
 * - Connecting to the AG-UI WebSocket at GET /ws/{session_id}
 * - Bidirectional decision round-trips (approvals, interrupts) over WS
 * - Session history and artifact queries
 *
 * All HTTPS requests include an Authorization header when
 * NEXT_PUBLIC_GATEWAY_TOKEN is set (production).  WebSocket connections
 * use a short-lived single-use ticket minted via POST /api/ws-ticket.
 *
 * See: docs/architecture_decisions/ADR-0075-websocket-transport.md
 */

import type { AGUIEvent, ClientMessage, SessionConfig } from './types';

/**
 * Base URL for the Seshat backend.
 *
 * Initialized to localhost for dev. In production, RuntimeConfigProvider
 * calls initAguiConfig() via useLayoutEffect before any child useEffect
 * fires, setting this to the value of SESHAT_URL from the runtime env (FRE-339).
 */
export let SESHAT_API = 'http://localhost:9000';

/**
 * Bearer token for gateway authentication.
 * Empty in dev — gateway auth is disabled locally. Set at runtime by
 * initAguiConfig() from GATEWAY_TOKEN env via the Server Component (FRE-339).
 */
let GATEWAY_TOKEN = '';

/**
 * Initialize the agui-client with runtime config values.
 *
 * Called by RuntimeConfigProvider (useLayoutEffect) before any child
 * useEffect runs, so all subsequent API calls use the correct URL and token.
 *
 * @param seshatUrl - Base URL for the Seshat backend.
 * @param gatewayToken - Bearer token for gateway auth (empty in dev).
 */
export function initAguiConfig(seshatUrl: string, gatewayToken: string): void {
  SESHAT_API = seshatUrl;
  GATEWAY_TOKEN = gatewayToken;
}

/** Returns auth headers when a token is configured; empty object otherwise. */
export function authHeaders(): Record<string, string> {
  return GATEWAY_TOKEN ? { Authorization: `Bearer ${GATEWAY_TOKEN}` } : {};
}

/** Derive WebSocket URL from the HTTP base URL. */
function wsBaseUrl(): string {
  const url = new URL(SESHAT_API);
  url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
  return url.origin;
}

// --------------------------------------------------------------------------
// Chat message dispatch
// --------------------------------------------------------------------------

// --------------------------------------------------------------------------
// User uploads (FRE-369 / ADR-0069)
// --------------------------------------------------------------------------

/** Metadata for an attachment that has completed the presign→upload→complete flow. */
export interface CompletedUpload {
  artifact_id: string;
  content_type: string;
  title: string;
}

/**
 * An attachment attached to an outgoing chat turn.
 *
 * ADR-0121 T5 (FRE-920): vision is a pinned Layer-3 role now — there is no
 * per-attachment local/cloud override to carry.
 */
export type UploadedAttachment = CompletedUpload;

/** Per-file upload state tracked by ChatInput. */
export interface UploadState {
  /** Client-side UUID for list key (distinct from artifact_id, which is set post-presign). */
  id: string;
  file: File;
  status: 'uploading' | 'complete' | 'error';
  artifact_id?: string;
  error?: string;
}

interface _PresignResponse {
  artifact_id: string;
  upload_url: string;
  expires_in: number;
}

/**
 * Mint a presigned R2 PUT URL for ``file``.
 *
 * @returns The presign response with artifact_id and upload_url.
 * @throws Error when the backend rejects the request (415, 413, 502, …).
 */
export async function presignUpload(file: File): Promise<_PresignResponse> {
  const resp = await fetch(`${SESHAT_API}/api/uploads/presign`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'include',
    body: JSON.stringify({
      filename: file.name,
      content_type: file.type,
      size_hint: file.size,
    }),
  });
  if (!resp.ok) {
    throw new Error(`Presign failed: ${resp.status} ${resp.statusText}`);
  }
  return resp.json() as Promise<_PresignResponse>;
}

/**
 * Upload ``file`` bytes directly to R2 via the presigned PUT URL.
 *
 * @throws Error on non-2xx R2 response.
 */
export async function uploadToR2(uploadUrl: string, file: File): Promise<void> {
  const resp = await fetch(uploadUrl, {
    method: 'PUT',
    headers: { 'Content-Type': file.type },
    body: file,
  });
  if (!resp.ok) {
    throw new Error(`R2 upload failed: ${resp.status}`);
  }
}

/**
 * Call /complete to verify the R2 object and clear upload_pending.
 *
 * @returns The completed attachment metadata.
 * @throws Error when backend returns 404 / 502 / 413.
 */
export async function completeUpload(artifactId: string): Promise<CompletedUpload> {
  const resp = await fetch(`${SESHAT_API}/api/uploads/${artifactId}/complete`, {
    method: 'POST',
    credentials: 'include',
  });
  if (!resp.ok) {
    throw new Error(`Complete failed: ${resp.status} ${resp.statusText}`);
  }
  const body = (await resp.json()) as { artifact_id: string; content_type: string; title: string };
  return { artifact_id: body.artifact_id, content_type: body.content_type, title: body.title };
}

// No cancelUpload: cancellation is client-side only (remove chip).
// Pending rows are cleaned up server-side by the expiry background task.

export interface SendMessageOptions {
  message: string;
  sessionId: string;
  /**
   * The client's selected `primary` model (ADR-0121 §4). Used by the server
   * only to establish a brand-new session's selection; ignored for an
   * existing session, whose stored value is authoritative (ADR-0079
   * invariants, carried forward). Sending it ensures a new session honours
   * the user's picker choice instead of silently adopting the default.
   */
  primarySelection?: string;
  /** Client-generated idempotency key (UUID v4) — deduplicated server-side (FRE-392). */
  clientMsgId?: string;
  /** Completed uploads to attach to this turn (FRE-369). */
  attachments?: UploadedAttachment[];
}

/**
 * Structured Cost-Gate denial (ADR-0065 / FRE-306).
 *
 * Thrown by sendChatMessage when the backend returns 503 with the
 * documented `error: "budget_denied"` body.
 */
export class BudgetDeniedError extends Error {
  readonly role: string;
  readonly timeWindow: string;
  readonly cap: string;
  readonly spend: string;
  readonly resetTime: string;
  readonly denialReason: string;

  constructor(payload: {
    role: string;
    time_window: string;
    cap: string;
    spend: string;
    reset_time: string;
    denial_reason: string;
  }) {
    super(`Budget denied for ${payload.role} (${payload.time_window})`);
    this.name = 'BudgetDeniedError';
    this.role = payload.role;
    this.timeWindow = payload.time_window;
    this.cap = payload.cap;
    this.spend = payload.spend;
    this.resetTime = payload.reset_time;
    this.denialReason = payload.denial_reason;
  }
}

/**
 * Send a chat message to the Seshat backend.
 *
 * Uses form-encoded body to match the existing FastAPI /chat/stream endpoint.
 * The backend pushes events to the WS stream identified by sessionId.
 *
 * @throws BudgetDeniedError when the backend returns 503 with a
 *   `error: "budget_denied"` payload.
 * @throws Error for any other non-2xx response.
 */
export async function sendChatMessage(opts: SendMessageOptions): Promise<void> {
  // ADR-0121 §4: the selection is server-owned. We still send the client's
  // picker choice so a NEW session is established with it; the server ignores
  // it for an existing session (stored value wins). The picker's canonical
  // mutator is setSessionSelection (PATCH .../selection).
  const { message, sessionId, primarySelection, clientMsgId, attachments } = opts;

  const params: Record<string, string> = { message, session_id: sessionId };
  if (primarySelection) {
    params['primary_selection'] = primarySelection;
  }
  if (clientMsgId) {
    params['client_msg_id'] = clientMsgId;
  }
  if (attachments && attachments.length > 0) {
    params['attachments'] = JSON.stringify(attachments);
  }

  const resp = await fetch(`${SESHAT_API}/chat/stream`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/x-www-form-urlencoded',
      ...authHeaders(),
    },
    body: new URLSearchParams(params),
  });

  if (!resp.ok) {
    if (resp.status === 503) {
      try {
        const body = await resp.json();
        if (body && body.error === 'budget_denied') {
          throw new BudgetDeniedError(body);
        }
      } catch (e) {
        if (e instanceof BudgetDeniedError) throw e;
      }
    }
    throw new Error(`Seshat /chat/stream returned ${resp.status}: ${resp.statusText}`);
  }
}

// --------------------------------------------------------------------------
// WebSocket ticket
// --------------------------------------------------------------------------

/**
 * Mint a short-lived single-use WebSocket ticket over HTTPS.
 *
 * In local dev (no GATEWAY_TOKEN) the server doesn't require a ticket,
 * so we return an empty string.
 */
async function getWSTicket(sessionId: string): Promise<string> {
  if (!GATEWAY_TOKEN) return '';

  const resp = await fetch(`${SESHAT_API}/api/ws-ticket`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      ...authHeaders(),
    },
    body: JSON.stringify({ session_id: sessionId }),
  });

  if (!resp.ok) {
    throw new Error(`ws-ticket failed: ${resp.status} ${resp.statusText}`);
  }
  const body = await resp.json();
  return body.ticket as string;
}

// --------------------------------------------------------------------------
// WebSocket connection (ADR-0075)
// --------------------------------------------------------------------------

export type AGUIEventHandler = (event: AGUIEvent) => void;
export type ErrorHandler = (error: Event) => void;

/** Code 4001 = "Superseded by new connection" — do not reconnect. */
const WS_CLOSE_SUPERSEDED = 4001;

export interface StreamConnection {
  close: () => void;
  send: (msg: ClientMessage) => void;
}

/** Optional lifecycle callbacks for connectWebSocket (FRE-236). */
export interface ConnectWebSocketOpts {
  /** Called when the WebSocket opens (initial connect or reconnect). */
  onWsConnected?: () => void;
  /** Called when the WebSocket closes unexpectedly (not intentional, not superseded). */
  onWsDisconnected?: () => void;
}

/**
 * Connect to the AG-UI WebSocket for a session.
 *
 * Handles:
 * - Ticket-based auth (mints a fresh ticket for each connection attempt)
 * - CONNECT handshake with last_seq for reconnect replay
 * - Application-level PING heartbeat every 25s
 * - Exponential backoff reconnect with jitter (1s..30s)
 * - localStorage persistence of last_seq
 * - Page visibility integration (persist last_seq on pagehide)
 *
 * @param sessionId - Target session to stream.
 * @param onEvent   - Called for each AG-UI event received.
 * @param onError   - Called on connection errors.
 * @returns StreamConnection with close() and send() methods.
 */
export function connectWebSocket(
  sessionId: string,
  onEvent: AGUIEventHandler,
  onError?: ErrorHandler,
  opts?: ConnectWebSocketOpts,
): StreamConnection {
  let ws: WebSocket | null = null;
  let pingInterval: ReturnType<typeof setInterval> | null = null;
  let closed = false;
  let backoffMs = 1000;
  let reconnectTimeout: ReturnType<typeof setTimeout> | null = null;
  let connecting = false;
  let connectGeneration = 0;

  // Keep same key for backward compat — semantics change from max-seen to
  // last-dispatched (ackSeq). Safe: conservative reconnect watermark on first
  // reconnect after upgrade (server replays from the stored value).
  const seqKey = `seshat_last_seq_${sessionId}`;

  // FRE-236: track when we went hidden with the WS open so we can include
  // hidden_duration_ms in the next CONNECT payload for telemetry.
  let hiddenAt: number | null = null;

  function getAckSeq(): number {
    if (typeof localStorage === 'undefined') return 0;
    const stored = localStorage.getItem(seqKey);
    return stored ? parseInt(stored, 10) || 0 : 0;
  }

  function setAckSeq(seq: number): void {
    if (typeof localStorage === 'undefined') return;
    localStorage.setItem(seqKey, String(seq));
  }

  // Out-of-order buffer: keyed by seq, cleared on every reconnect.
  // Prevents the FRE-518 failure mode where seq=2 arriving before seq=1 caused
  // seq=1 to be permanently dropped (and replayed from wrong watermark).
  const pendingBuf = new Map<number, AGUIEvent>();

  function persistSeqOnHide(): void {
    // last_seq is already persisted on each event; this is a safety net
    // for iOS PWA suspension where the event loop may not run.
    // FRE-236: also record when we went hidden with an open WS for telemetry.
    if (ws?.readyState === WebSocket.OPEN) {
      hiddenAt = Date.now();
    }
  }

  async function connect(): Promise<void> {
    if (closed) return;
    if (
      connecting ||
      ws?.readyState === WebSocket.CONNECTING ||
      ws?.readyState === WebSocket.OPEN
    ) {
      return;
    }

    connecting = true;
    const generation = ++connectGeneration;
    pendingBuf.clear();

    try {
      const ticket = await getWSTicket(sessionId);
      if (closed || generation !== connectGeneration) return;

      const base = wsBaseUrl();
      const ticketParam = ticket ? `?ticket=${encodeURIComponent(ticket)}` : '';
      const url = `${base}/ws/${encodeURIComponent(sessionId)}${ticketParam}`;

      ws = new WebSocket(url);

      ws.onopen = () => {
        if (closed || generation !== connectGeneration) {
          ws?.close();
          return;
        }
        backoffMs = 1000;
        const lastSeq = getAckSeq();
        // FRE-236: include hidden_duration_ms when reconnecting after a visibility hide.
        const connectPayload: Record<string, unknown> = { type: 'CONNECT', last_seq: lastSeq };
        if (hiddenAt !== null) {
          connectPayload['hidden_duration_ms'] = Date.now() - hiddenAt;
          hiddenAt = null;
        }
        ws?.send(JSON.stringify(connectPayload));

        // Start PING heartbeat
        if (pingInterval) clearInterval(pingInterval);
        pingInterval = setInterval(() => {
          if (ws?.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify({ type: 'PING' }));
          }
        }, 25000);

        // FRE-236: notify the hook that the WS is (re)connected.
        opts?.onWsConnected?.();
      };

      ws.onmessage = (ev: MessageEvent) => {
        try {
          const parsed = JSON.parse(ev.data as string) as AGUIEvent;
          if (parsed.seq != null) {
            const seq = parsed.seq;
            const ackSeq = getAckSeq();
            if (seq <= ackSeq || pendingBuf.has(seq)) return; // duplicate
            pendingBuf.set(seq, parsed);
            // Drain contiguous run starting from ackSeq+1
            let next = ackSeq + 1;
            while (pendingBuf.has(next)) {
              onEvent(pendingBuf.get(next)!);
              pendingBuf.delete(next);
              setAckSeq(next);
              next = getAckSeq() + 1;
            }
            return;
          }
          // seq == null: DONE, PONG, REPLAY_GAP
          if (parsed.type === 'DONE' && getAckSeq() === 0 && pendingBuf.size > 0) {
            // Cold-start fallback only (ackSeq===0): global Postgres seq may not
            // start at ackSeq+1 (e.g. fresh client with ackSeq=0 but first event
            // has seq=5000). For ackSeq>0, leave the buffer so reconnect replay
            // can fill the genuine gap — do NOT advance ackSeq past the hole.
            const sortedKeys = [...pendingBuf.keys()].sort((a, b) => a - b);
            for (const k of sortedKeys) {
              onEvent(pendingBuf.get(k)!);
              setAckSeq(k);
            }
            pendingBuf.clear();
          }
          onEvent(parsed);
        } catch {
          // Malformed message — skip
        }
      };

      ws.onclose = (ev: CloseEvent) => {
        cleanup();
        if (generation !== connectGeneration) return;
        if (closed || ev.code === WS_CLOSE_SUPERSEDED) return;
        // FRE-236: notify the hook of an unexpected disconnect.
        opts?.onWsDisconnected?.();
        scheduleReconnect();
      };

      ws.onerror = () => {
        if (onError) onError(new Event('error'));
      };

    } catch {
      // Ticket fetch or connection setup failed
      if (!closed) scheduleReconnect();
    } finally {
      if (generation === connectGeneration) {
        connecting = false;
      }
    }
  }

  function scheduleReconnect(): void {
    if (closed) return;
    if (reconnectTimeout) return;
    const jitter = Math.random() * 500;
    reconnectTimeout = setTimeout(() => {
      reconnectTimeout = null;
      void connect();
    }, backoffMs + jitter);
    backoffMs = Math.min(backoffMs * 2, 30000);
  }

  function cleanup(): void {
    if (pingInterval) {
      clearInterval(pingInterval);
      pingInterval = null;
    }
  }

  // Page visibility integration
  const handleVisibilityChange = () => {
    if (document.visibilityState === 'hidden') {
      persistSeqOnHide();
    } else if (document.visibilityState === 'visible' && !closed) {
      // Reconnect on return from background
      if (!ws || ws.readyState === WebSocket.CLOSED) {
        void connect();
      }
    }
  };

  if (typeof document !== 'undefined') {
    document.addEventListener('visibilitychange', handleVisibilityChange);

    window.addEventListener('pagehide', persistSeqOnHide);
  }

  // Start initial connection
  void connect();

  return {
    close: () => {
      closed = true;
      connecting = false;
      connectGeneration += 1;
      cleanup();
      if (reconnectTimeout) clearTimeout(reconnectTimeout);
      reconnectTimeout = null;
      if (typeof document !== 'undefined') {
        document.removeEventListener('visibilitychange', handleVisibilityChange);
        window.removeEventListener('pagehide', persistSeqOnHide);
      }
      if (ws) {
        ws.onclose = null;
        ws.close();
        ws = null;
      }
    },
    send: (msg: ClientMessage) => {
      if (ws?.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify(msg));
      }
    },
  };
}

// --------------------------------------------------------------------------
// Session history
// --------------------------------------------------------------------------

/** Summary of a persisted session from GET /api/v1/sessions(/:id). */
export interface SessionSummary {
  session_id: string;
  created_at: string;
  last_active_at: string;
  mode: string;
  channel: string | null;
  /** Server-authoritative `primary` model selection (ADR-0121 §4). */
  primary_selection: string;
  /** How `primary_selection` was resolved — `stored` / `adopted` / `default`. */
  selection_provenance: string;
  message_count: number;
  /** Number of user turns (user-role messages only) in this session (FRE-521). */
  turn_count?: number;
  title: string | null;
  /** Current context size + window for status-bar hydration (FRE-426). */
  context_tokens?: number;
  context_max?: number;
  /** Accumulated session cost in USD for status-bar hydration (FRE-426). */
  cost_usd?: number;
}

/** A single persisted message from GET /api/v1/sessions/{id}/messages. */
export interface ServerMessage {
  role: string;
  content: string;
  timestamp?: string;
  trace_id?: string;
  metadata?: Record<string, unknown>;
  /** Previously-submitted 0–3 rating, joined from user-turn-ratings (FRE-426). */
  rating?: number;
}

/**
 * List recent sessions from the backend.
 *
 * @param limit - Maximum number of sessions to return (default 20).
 * @returns Array of session summaries, most-recent first.
 * @throws Error when the backend returns a non-2xx status.
 */
export async function listSessions(limit = 20): Promise<SessionSummary[]> {
  const resp = await fetch(
    `${SESHAT_API}/api/v1/sessions?limit=${limit}`,
    { headers: authHeaders() },
  );
  if (!resp.ok) throw new Error(`listSessions failed: ${resp.status}`);
  return resp.json() as Promise<SessionSummary[]>;
}

/**
 * Fetch the message history for a session.
 *
 * Returns an empty array when the session does not exist (404) so callers
 * can treat it as a fresh session without special-casing.
 *
 * @param sessionId - The session to fetch messages for.
 * @param limit     - Maximum number of messages to return (default 200).
 * @returns Array of server messages in chronological order.
 * @throws Error when the backend returns a non-2xx, non-404 status.
 */
export async function getSessionMessages(
  sessionId: string,
  limit = 200,
): Promise<ServerMessage[]> {
  const resp = await fetch(
    `${SESHAT_API}/api/v1/sessions/${encodeURIComponent(sessionId)}/messages?limit=${limit}`,
    { headers: authHeaders() },
  );
  if (resp.status === 404) return [];
  if (!resp.ok) throw new Error(`getSessionMessages failed: ${resp.status}`);
  return resp.json() as Promise<ServerMessage[]>;
}

/**
 * Fetch a single session, including its server-authoritative
 * `primary_selection` (ADR-0121 §4). Used on mount to hydrate the model
 * picker from the server instead of client-only localStorage.
 *
 * @param sessionId - The session to fetch.
 * @returns The session detail, or null when it does not exist yet (404).
 * @throws Error when the backend returns a non-2xx, non-404 status.
 */
export async function getSession(sessionId: string): Promise<SessionSummary | null> {
  const resp = await fetch(
    `${SESHAT_API}/api/v1/sessions/${encodeURIComponent(sessionId)}`,
    { headers: authHeaders() },
  );
  if (resp.status === 404) return null;
  if (!resp.ok) throw new Error(`getSession failed: ${resp.status}`);
  return resp.json() as Promise<SessionSummary>;
}

/**
 * Fetch the model-picker + observe-view read payload for a session
 * (ADR-0121 §3 — `GET /api/v1/sessions/{id}/config`).
 *
 * 404s until the session's first DB row exists (created on first message) —
 * use {@link getConfig} for a brand-new conversation instead.
 *
 * @param sessionId - The session to fetch config for.
 * @returns The config payload, or null when the session doesn't exist yet (404).
 * @throws Error when the backend returns a non-2xx, non-404 status.
 */
export async function getSessionConfig(sessionId: string): Promise<SessionConfig | null> {
  const resp = await fetch(
    `${SESHAT_API}/api/v1/sessions/${encodeURIComponent(sessionId)}/config`,
    { headers: authHeaders() },
  );
  if (resp.status === 404) return null;
  if (!resp.ok) throw new Error(`getSessionConfig failed: ${resp.status}`);
  return resp.json() as Promise<SessionConfig>;
}

/**
 * Fetch the sessionless model-picker + observe-view read payload
 * (ADR-0121 T5, FRE-920 — `GET /api/v1/config`).
 *
 * Same `roles`/`providers` shape as {@link getSessionConfig} minus the
 * per-session `resolved`/`provenance` fields — used for a brand-new
 * conversation before its first message creates a DB row.
 *
 * @returns The sessionless config payload.
 * @throws Error when the backend returns a non-2xx status.
 */
export async function getConfig(): Promise<SessionConfig> {
  const resp = await fetch(`${SESHAT_API}/api/v1/config`, { headers: authHeaders() });
  if (!resp.ok) throw new Error(`getConfig failed: ${resp.status}`);
  return resp.json() as Promise<SessionConfig>;
}

/**
 * Set a session's server-authoritative `primary` model selection (ADR-0121 §4).
 *
 * This is the canonical write for the model picker: it persists the value
 * on the session and triggers a `session_selection` STATE_DELTA to the
 * active client.
 *
 * @param sessionId    - The session to update.
 * @param role         - The role to set (only `"primary"` is user-selectable today).
 * @param deploymentKey - The new deployment key.
 * @throws Error when the backend returns a non-2xx status.
 */
export async function setSessionSelection(
  sessionId: string,
  role: string,
  deploymentKey: string,
): Promise<void> {
  const resp = await fetch(
    `${SESHAT_API}/api/v1/sessions/${encodeURIComponent(sessionId)}/selection`,
    {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json', ...authHeaders() },
      body: JSON.stringify({ role, deployment_key: deploymentKey }),
    },
  );
  if (!resp.ok) throw new Error(`setSessionSelection failed: ${resp.status}`);
}

// --------------------------------------------------------------------------
// FRE-230 — Location preference helpers
// --------------------------------------------------------------------------

/** Operator + per-user location gates returned by the preferences endpoint. */
export interface LocationPreference {
  /** Deployment-wide operator gate (AGENT_LOCATION_ENABLED). */
  feature_enabled: boolean;
  /** Per-user consent gate stored on the :Person node. */
  location_consent_enabled: boolean;
}

/** Optional client-provided coordinates + browser timezone for a consent update. */
export interface LocationCoordinates {
  latitude: number;
  longitude: number;
  timezone: string;
}

/**
 * Read the authenticated user's location gates (FRE-230).
 *
 * `feature_enabled` reflects the operator gate; when false the PWA hides the
 * consent toggle entirely. `location_consent_enabled` is the user's own opt-in.
 *
 * @throws Error when the backend returns a non-2xx status.
 */
export async function getLocationPreference(): Promise<LocationPreference> {
  const resp = await fetch(`${SESHAT_API}/api/v1/preferences/location`, {
    headers: authHeaders(),
  });
  if (!resp.ok) throw new Error(`getLocationPreference failed: ${resp.status}`);
  return resp.json() as Promise<LocationPreference>;
}

/**
 * Update the user's location consent and optionally store device coordinates
 * (FRE-230). Coordinates are only persisted server-side when consent is true.
 *
 * @param consentEnabled - New consent value, or undefined to leave unchanged.
 * @param coords - Device coordinates + IANA timezone, or undefined to skip.
 * @throws Error when the backend returns a non-2xx status (e.g. 403 when the
 *   operator gate is disabled).
 */
export async function updateLocationPreference(
  consentEnabled?: boolean,
  coords?: LocationCoordinates,
): Promise<LocationPreference> {
  const body: {
    consent_enabled?: boolean;
    latitude?: number;
    longitude?: number;
    timezone?: string;
  } = {};
  if (consentEnabled !== undefined) body.consent_enabled = consentEnabled;
  if (coords) {
    body.latitude = coords.latitude;
    body.longitude = coords.longitude;
    body.timezone = coords.timezone;
  }
  const resp = await fetch(`${SESHAT_API}/api/v1/preferences/location`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json', ...authHeaders() },
    body: JSON.stringify(body),
  });
  if (!resp.ok) throw new Error(`updateLocationPreference failed: ${resp.status}`);
  return resp.json() as Promise<LocationPreference>;
}

// --------------------------------------------------------------------------
// FRE-368 — Artifact helpers
// --------------------------------------------------------------------------

/** Public-facing metadata for a single artifact (no r2_key, no embedding). */
export interface ArtifactSummary {
  artifact_id: string;
  public_url: string | null;
  slug: string | null;
  title: string | null;
  summary: string | null;
  content_type: string;
  size_bytes: number;
  tags: string[];
  created_at: string;
}

export interface ListArtifactsOptions {
  type?: 'artifact' | 'note' | 'upload' | 'capture';
  prefix?: string;
  k?: number;
  since?: string;
}

/**
 * List the authenticated user's artifacts, newest first.
 *
 * CF Access JWT is injected by the CF edge for browser requests —
 * no manual header setting required.
 *
 * @throws Error when the backend returns a non-2xx status.
 */
export async function listArtifacts(
  opts: ListArtifactsOptions = {},
): Promise<ArtifactSummary[]> {
  const params = new URLSearchParams();
  if (opts.type) params.set('type', opts.type);
  if (opts.prefix) params.set('prefix', opts.prefix);
  if (opts.k !== undefined) params.set('k', String(opts.k));
  if (opts.since) params.set('since', opts.since);

  const qs = params.toString();
  const resp = await fetch(
    `${SESHAT_API}/api/v1/artifacts${qs ? `?${qs}` : ''}`,
    { headers: authHeaders() },
  );
  if (!resp.ok) throw new Error(`listArtifacts failed: ${resp.status}`);
  const body = await resp.json() as { items: ArtifactSummary[] };
  return body.items;
}

/**
 * Fetch metadata for a single artifact by ID.
 *
 * Returns null when the artifact is not found (404) or belongs to another user.
 *
 * @throws Error for non-2xx, non-404 responses.
 */
export async function getArtifactMetadata(
  artifactId: string,
): Promise<ArtifactSummary | null> {
  const resp = await fetch(
    `${SESHAT_API}/api/v1/artifacts/${encodeURIComponent(artifactId)}`,
    { headers: authHeaders() },
  );
  if (resp.status === 404) return null;
  if (!resp.ok) throw new Error(`getArtifactMetadata failed: ${resp.status}`);
  return resp.json() as Promise<ArtifactSummary>;
}

/**
 * Fire-and-forget card-click telemetry for ADR-0070 D8 measurement.
 *
 * Never throws — telemetry must never break the user interaction.
 */
export function postCardClick(
  artifactId: string,
  surface: 'inline' | 'drawer' | 'standalone',
  sessionId?: string,
): void {
  const url = `${SESHAT_API}/api/v1/telemetry/card_click`;
  const body = JSON.stringify({
    artifact_id: artifactId,
    kind: 'card_click',
    surface,
    ...(sessionId ? { session_id: sessionId } : {}),
  });

  try {
    if (typeof navigator !== 'undefined' && 'sendBeacon' in navigator) {
      const blob = new Blob([body], { type: 'application/json' });
      navigator.sendBeacon(url, blob);
      return;
    }
  } catch {
    // sendBeacon not available or failed — fall through to fetch
  }

  void fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...authHeaders() },
    body,
    keepalive: true,
  }).catch(() => {
    // Best-effort — swallow all errors
  });
}

// --------------------------------------------------------------------------
// FRE-549 — Artifact export (wires the FRE-530 /export endpoint)
// --------------------------------------------------------------------------

/** Export modes accepted by the backend (ADR-0089 A5, FRE-530). */
export type ArtifactExportMode = 'inline' | 'substitute';

/**
 * Raised by {@link fetchArtifactExport} on a non-2xx response.
 *
 * Carries the exact HTTP status so the UI can branch — notably `502` (inline
 * asset fetch / SRI failure, e.g. before the CF service token is authorized)
 * versus any other failure.
 */
export class ArtifactExportError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = 'ArtifactExportError';
    this.status = status;
  }
}

/**
 * Fetch a standalone export of an HTML artifact (FRE-530 endpoint).
 *
 * Follows the existing PWA fetch pattern — `authHeaders()` only, with the CF
 * Access JWT injected by the edge — so no `credentials` flag is set. Returns
 * the response body as a Blob for download; the caller supplies the filename.
 *
 * @param artifactId - The artifact to export.
 * @param mode - `inline` (offline-portable) or `substitute` (CDN + SRI).
 * @returns The exported HTML as a Blob.
 * @throws ArtifactExportError carrying the HTTP status on any non-2xx response.
 */
export async function fetchArtifactExport(
  artifactId: string,
  mode: ArtifactExportMode,
): Promise<Blob> {
  const resp = await fetch(
    `${SESHAT_API}/api/v1/artifacts/${encodeURIComponent(artifactId)}/export?mode=${mode}`,
    { headers: authHeaders() },
  );
  if (!resp.ok) {
    throw new ArtifactExportError(
      resp.status,
      `artifact export failed: ${resp.status} ${resp.statusText}`,
    );
  }
  return resp.blob();
}
