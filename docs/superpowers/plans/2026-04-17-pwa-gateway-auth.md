# PWA Gateway Auth Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Enable `AGENT_GATEWAY_AUTH_ENABLED=true` so the gateway validates Bearer tokens on all endpoints, without breaking the PWA's SSE stream.

**Architecture:** Replace the PWA's `EventSource`-based `connectToStream()` with `@microsoft/fetch-event-source` (3.2KB gzipped, zero transitive deps), which uses `fetch()` internally and supports custom headers. Add `Authorization: Bearer ${token}` to all three fetch call sites in `agui-client.ts`. The `StreamConnection` interface is preserved — `useSSEStream.ts` needs no changes. The Bearer token is baked into the PWA bundle at build time via `NEXT_PUBLIC_GATEWAY_TOKEN` (same value as `GATEWAY_TOKEN_PWA` on the gateway side).

**Tech Stack:** `@microsoft/fetch-event-source`, Next.js 15, Docker Compose

**Prerequisite:** The Cloudflare Tunnel plan (`2026-04-17-cloudflare-tunnel-terraform.md`) must be fully deployed and verified before running this plan. The gateway must be reachable at `https://agent.frenchforet.com`.

---

## Security Model

`NEXT_PUBLIC_GATEWAY_TOKEN` is embedded in the Next.js JS bundle and visible to anyone who inspects the page source. This is acceptable for a low-privilege `pwa-client` token (scopes: `knowledge:read`, `sessions:read`, `observations:read` — read-only). Do not grant this token write scopes. High-privilege operations (writes, observations) should only be accessible to external agents using their own token at `api.frenchforet.com`.

---

## Pre-flight Checklist

- [ ] Cloudflare Tunnel plan fully deployed (`https://agent.frenchforet.com` loads)
- [ ] VPS `.env` accessible via SSH

---

## File Map

**Modified files:**

| File | Change |
|---|---|
| `seshat-pwa/package.json` | Add `@microsoft/fetch-event-source` |
| `seshat-pwa/package-lock.json` | Updated by npm install |
| `seshat-pwa/src/lib/agui-client.ts` | Replace `EventSource` with `fetchEventSource`; add auth headers to all three fetch calls |
| `Dockerfile.pwa` | Add `NEXT_PUBLIC_GATEWAY_TOKEN` build arg + ENV |
| `docker-compose.cloud.yml` | Add `NEXT_PUBLIC_GATEWAY_TOKEN` build arg to `seshat-pwa`; set `AGENT_GATEWAY_AUTH_ENABLED: "true"` on `seshat-gateway` |

**No changes to:**
- `seshat-pwa/src/hooks/useSSEStream.ts` — `StreamConnection` interface preserved
- `config/gateway_access.yaml` — `pwa-client` entry already exists with `${GATEWAY_TOKEN_PWA}`
- Gateway Python code — auth middleware already fully implemented

---

## Task 1: Install fetch-event-source and update agui-client.ts

**Files:**
- Modify: `seshat-pwa/package.json` + `seshat-pwa/package-lock.json`
- Modify: `seshat-pwa/src/lib/agui-client.ts`

- [ ] **Step 1: Install the library**

```bash
cd seshat-pwa
npm install @microsoft/fetch-event-source
cd ..
```

Expected: `added 1 package` in output. Verify in `package.json` that `@microsoft/fetch-event-source` appears in `dependencies`.

- [ ] **Step 2: Write the failing test (manual verification)**

Since this is a browser library there are no unit tests to write. Verification is end-to-end (Task 3). Instead, confirm the type-check baseline passes before making changes:

```bash
cd seshat-pwa
npx tsc --noEmit
cd ..
```

Expected: No errors. Note any pre-existing errors so you don't confuse them with regressions.

- [ ] **Step 3: Replace agui-client.ts**

Replace the full contents of `seshat-pwa/src/lib/agui-client.ts` with:

```typescript
/**
 * Low-level AG-UI client utilities.
 *
 * Provides helpers for interacting with the Seshat backend:
 * - Sending chat messages via POST /chat/stream
 * - Connecting to the AG-UI SSE stream at GET /stream/{session_id}
 * - Resuming HITL interrupts via POST /stream/{session_id}/resume
 *
 * All requests include an Authorization header when NEXT_PUBLIC_GATEWAY_TOKEN
 * is set (production). In local dev (token absent) the header is omitted and
 * the gateway's auth-disabled fast-path allows the request through.
 */

import { fetchEventSource } from '@microsoft/fetch-event-source';

import type { AGUIEvent } from './types';

/** Base URL for the Seshat backend. Defaults to localhost in dev. */
export const SESHAT_API =
  process.env.NEXT_PUBLIC_SESHAT_URL ?? 'http://localhost:9000';

/**
 * Bearer token for gateway authentication.
 * Baked into the bundle at build time via NEXT_PUBLIC_GATEWAY_TOKEN.
 * Empty string in local dev — gateway auth is disabled locally.
 */
const GATEWAY_TOKEN = process.env.NEXT_PUBLIC_GATEWAY_TOKEN ?? '';

/** Returns auth headers when a token is configured; empty object otherwise. */
function authHeaders(): Record<string, string> {
  return GATEWAY_TOKEN ? { Authorization: `Bearer ${GATEWAY_TOKEN}` } : {};
}

// --------------------------------------------------------------------------
// Chat message dispatch
// --------------------------------------------------------------------------

export interface SendMessageOptions {
  message: string;
  sessionId: string;
  profile?: string;
}

/**
 * Send a chat message to the Seshat backend.
 *
 * Uses form-encoded body to match the existing FastAPI /chat endpoint.
 * The backend emits events to the SSE stream identified by sessionId.
 *
 * @throws Error when the backend returns a non-2xx status.
 */
export async function sendChatMessage(opts: SendMessageOptions): Promise<void> {
  const { message, sessionId, profile = 'local' } = opts;

  const resp = await fetch(`${SESHAT_API}/chat/stream`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/x-www-form-urlencoded',
      ...authHeaders(),
    },
    body: new URLSearchParams({
      message,
      session_id: sessionId,
      profile,
    }),
  });

  if (!resp.ok) {
    throw new Error(`Seshat /chat/stream returned ${resp.status}: ${resp.statusText}`);
  }
}

// --------------------------------------------------------------------------
// SSE stream connection
// --------------------------------------------------------------------------

export type AGUIEventHandler = (event: AGUIEvent) => void;
export type ErrorHandler = (error: Event) => void;

export interface StreamConnection {
  /** Close the stream and stop receiving events. */
  close: () => void;
}

/**
 * Connect to the AG-UI SSE stream for a session.
 *
 * Uses fetch-event-source instead of the browser's EventSource so that
 * an Authorization header can be included. The StreamConnection interface
 * is identical to the previous EventSource-based implementation —
 * useSSEStream.ts needs no changes.
 *
 * Reconnection on transient network failures is handled automatically by
 * fetchEventSource. Deliberate close (via StreamConnection.close) and
 * server-side errors abort without retrying.
 *
 * @param sessionId - Target session to stream.
 * @param onEvent   - Called for each AG-UI event received.
 * @param onError   - Called on stream error (before close).
 * @returns StreamConnection with a close() method.
 */
export function connectToStream(
  sessionId: string,
  onEvent: AGUIEventHandler,
  onError?: ErrorHandler,
): StreamConnection {
  const ctrl = new AbortController();

  fetchEventSource(`${SESHAT_API}/stream/${encodeURIComponent(sessionId)}`, {
    headers: authHeaders(),
    signal: ctrl.signal,
    onmessage(ev) {
      try {
        const parsed = JSON.parse(ev.data) as AGUIEvent;
        onEvent(parsed);
      } catch {
        // Malformed event — skip silently; structlog on backend will have trace.
      }
    },
    onerror(err) {
      if (onError) onError(new Event('error'));
      // Rethrow to stop fetchEventSource's retry loop on errors.
      throw err;
    },
  }).catch(() => {
    // Swallow AbortError from ctrl.abort() and any onerror rethrows —
    // they are already surfaced to the caller via onError.
  });

  return {
    close: () => ctrl.abort(),
  };
}

// --------------------------------------------------------------------------
// HITL resume
// --------------------------------------------------------------------------

export interface ResumeOptions {
  sessionId: string;
  choice: string;
}

/**
 * Resume a HITL-interrupted session with the user's choice.
 *
 * The backend expects a POST to /stream/{session_id}/resume with a JSON body
 * containing the chosen option.
 *
 * @throws Error when the backend returns a non-2xx status.
 */
export async function resumeInterrupt(opts: ResumeOptions): Promise<void> {
  const { sessionId, choice } = opts;

  const resp = await fetch(
    `${SESHAT_API}/stream/${encodeURIComponent(sessionId)}/resume`,
    {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        ...authHeaders(),
      },
      body: JSON.stringify({ choice }),
    },
  );

  if (!resp.ok) {
    throw new Error(`Resume failed: ${resp.status} ${resp.statusText}`);
  }
}
```

- [ ] **Step 4: Run type-check**

```bash
cd seshat-pwa
npx tsc --noEmit
cd ..
```

Expected: No errors (same as baseline from Step 2).

- [ ] **Step 5: Commit**

```bash
git add seshat-pwa/package.json seshat-pwa/package-lock.json seshat-pwa/src/lib/agui-client.ts
git commit -m "feat(pwa): replace EventSource with fetchEventSource for Bearer auth support"
```

---

## Task 2: Add NEXT_PUBLIC_GATEWAY_TOKEN build arg to Dockerfile and docker-compose

**Files:**
- Modify: `Dockerfile.pwa`
- Modify: `docker-compose.cloud.yml`

- [ ] **Step 1: Update Dockerfile.pwa**

In `Dockerfile.pwa`, find the builder stage arg section:

```dockerfile
ARG NEXT_PUBLIC_SESHAT_URL=http://172.25.0.10
ENV NEXT_PUBLIC_SESHAT_URL=${NEXT_PUBLIC_SESHAT_URL}
```

Add the gateway token arg directly below:

```dockerfile
ARG NEXT_PUBLIC_SESHAT_URL=http://172.25.0.10
ENV NEXT_PUBLIC_SESHAT_URL=${NEXT_PUBLIC_SESHAT_URL}

ARG NEXT_PUBLIC_GATEWAY_TOKEN=""
ENV NEXT_PUBLIC_GATEWAY_TOKEN=${NEXT_PUBLIC_GATEWAY_TOKEN}
```

Default is empty string — in local dev the token is absent and gateway auth is disabled.

- [ ] **Step 2: Update docker-compose.cloud.yml — PWA build arg**

In `docker-compose.cloud.yml`, find the `seshat-pwa` build args section:

```yaml
      args:
        NEXT_PUBLIC_SESHAT_URL: "https://agent.frenchforet.com"
```

Add the token arg:

```yaml
      args:
        NEXT_PUBLIC_SESHAT_URL: "https://agent.frenchforet.com"
        NEXT_PUBLIC_GATEWAY_TOKEN: ${GATEWAY_TOKEN_PWA}
```

`GATEWAY_TOKEN_PWA` is read from the VPS `.env` at build time. It must equal the secret in `config/gateway_access.yaml`'s `pwa-client` entry.

- [ ] **Step 3: Update docker-compose.cloud.yml — enable gateway auth**

In the `seshat-gateway` service `environment` block, find:

```yaml
      AGENT_GATEWAY_AUTH_ENABLED: "false"
```

Change to:

```yaml
      AGENT_GATEWAY_AUTH_ENABLED: "true"
```

- [ ] **Step 4: Add GATEWAY_TOKEN_PWA to VPS .env**

SSH into the VPS:

```bash
ssh vps-5a0f676b
cd /opt/seshat
```

Generate a secure token:

```bash
openssl rand -hex 32
```

Add two entries to `.env` (both must be the same value):

```
GATEWAY_TOKEN_PWA=<output-from-openssl>
GATEWAY_TOKEN_EXTERNAL_AGENT=<separate-openssl-rand-hex-32-output>
```

Note: `NEXT_PUBLIC_GATEWAY_TOKEN` does NOT go in the VPS `.env` — it is a docker-compose build arg, resolved from `GATEWAY_TOKEN_PWA` at build time via the `${GATEWAY_TOKEN_PWA}` reference in `docker-compose.cloud.yml`.

- [ ] **Step 5: Commit**

```bash
git add Dockerfile.pwa docker-compose.cloud.yml
git commit -m "feat(pwa): add NEXT_PUBLIC_GATEWAY_TOKEN build arg; enable gateway auth in docker-compose"
```

---

## Task 3: Deploy and verify end-to-end

- [ ] **Step 1: Push changes to remote**

```bash
git push origin main
```

- [ ] **Step 2: Full rebuild deploy**

Both the PWA (new bundle with token) and the gateway (auth enabled) need rebuilding:

```bash
bash infrastructure/scripts/deploy.sh --full
```

Expected: All containers healthy.

- [ ] **Step 3: Verify PWA loads**

```bash
curl -I https://agent.frenchforet.com
```

Expected: `HTTP/2 200`

- [ ] **Step 4: Verify SSE stream works**

Open `https://agent.frenchforet.com` in a browser. Send a chat message. Confirm a response streams back. In DevTools → Network tab, confirm:
- `/chat/stream` POST → 200, `Authorization: Bearer <token>` present in request headers
- `/stream/{id}` GET → 200 (EventSource replaced by fetch-event-source), `Authorization` header present

If `/stream/{id}` returns 401, the token baked into the bundle doesn't match what the gateway expects. Check that `GATEWAY_TOKEN_PWA` in VPS `.env` matches the value used at build time.

- [ ] **Step 5: Verify unauthenticated request is rejected by gateway**

```bash
curl -s -o /dev/null -w "%{http_code}" https://agent.frenchforet.com/api/health
```

Expected: `401` (gateway rejects request with no Authorization header; this comes from the gateway, not the WAF, since `agent.frenchforet.com` is not covered by the WAF rule)

- [ ] **Step 6: Verify external agent access**

```bash
# Replace <your-external-agent-token> with GATEWAY_TOKEN_EXTERNAL_AGENT from VPS .env
curl -s -o /dev/null -w "%{http_code}" \
  -H "Authorization: Bearer <your-external-agent-token>" \
  https://api.frenchforet.com/health
```

Expected: `200`

```bash
# No token — WAF blocks before reaching gateway
curl -s -o /dev/null -w "%{http_code}" https://api.frenchforet.com/health
```

Expected: `403` (Cloudflare WAF)
