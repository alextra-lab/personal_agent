/**
 * Shared helpers for Playwright e2e tests (FRE-400 WS3).
 *
 * All tests mock the Seshat backend entirely — no live server required.
 * The WebSocket is intercepted via page.routeWebSocket(); REST endpoints
 * via page.route().
 */

import type { Page, WebSocketRoute } from '@playwright/test';

/** Fixed session ID used across tests (deterministic URL + easy to mock). */
export const TEST_SESSION = '00000000-0000-0000-0000-000000000e2e';

/** Base URL for the mock Seshat backend (baked in via NEXT_PUBLIC_SESHAT_URL). */
const BACKEND = 'http://localhost:9000';

/**
 * Stub all required Seshat REST endpoints so the page can mount without errors.
 *
 * - sessions/{id}/messages → empty history (new session)
 * - sessions/{id}          → 404 (no server-side profile to hydrate)
 * - inference/status       → "up" so Send is enabled
 * - chat/stream            → 200 OK (fire-and-forget; WS carries events)
 */
export async function stubRest(page: Page, sessionId = TEST_SESSION): Promise<void> {
  await page.route(`${BACKEND}/api/v1/sessions/${sessionId}/messages*`, (route) =>
    route.fulfill({ json: [] }),
  );
  await page.route(`${BACKEND}/api/v1/sessions/${sessionId}`, (route) =>
    route.fulfill({ status: 404, body: 'not found' }),
  );
  await page.route(`${BACKEND}/api/inference/status*`, (route) =>
    route.fulfill({ json: { status: 'up', latency_ms: 10 } }),
  );
  await page.route(`${BACKEND}/chat/stream`, (route) =>
    route.fulfill({ status: 200, body: '' }),
  );
}

/**
 * Register a WebSocket mock for `/ws/{sessionId}` and return a promise that
 * resolves with the `WebSocketRoute` the first time a connection is established.
 *
 * The resolved route's `.send()` method pushes frames to the client.
 * Incoming client messages (CONNECT, CONSTRAINT_DECISION, USER_CANCEL, etc.)
 * are accumulated in `received`.
 */
export async function stubWebSocket(
  page: Page,
  sessionId = TEST_SESSION,
): Promise<{ wsReady: Promise<WebSocketRoute>; received: string[] }> {
  const received: string[] = [];
  let resolveWsReady!: (route: WebSocketRoute) => void;
  const wsReady = new Promise<WebSocketRoute>((resolve) => {
    resolveWsReady = resolve;
  });

  await page.routeWebSocket(`ws://localhost:9000/ws/${sessionId}`, (ws) => {
    resolveWsReady(ws);
    ws.onMessage((msg) => {
      received.push(typeof msg === 'string' ? msg : msg.toString());
    });
  });

  return { wsReady, received };
}

/** Send a JSON frame from the mock server to the browser client. */
export function serverSend(ws: WebSocketRoute, payload: object): void {
  ws.send(JSON.stringify(payload));
}

/** Type a message and click Send (waits for Send button to be enabled). */
export async function sendChatMessage(page: Page, text: string): Promise<void> {
  await page.fill('[placeholder="Message Seshat..."]', text);
  await page.click('[aria-label="Send message"]');
}
