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
 * - chat/stream            → 200 OK (fire-and-forget; WS carries events)
 */
export async function stubRest(page: Page, sessionId = TEST_SESSION): Promise<void> {
  await page.route(`${BACKEND}/api/v1/sessions/${sessionId}/messages*`, (route) =>
    route.fulfill({ json: [] }),
  );
  await page.route(`${BACKEND}/api/v1/sessions/${sessionId}`, (route) =>
    route.fulfill({ status: 404, body: 'not found' }),
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

// ---------------------------------------------------------------------------
// Contrast helpers (FRE-1264 AC-7 / FRE-1265 AC-2) — real rendered CSS is
// required here since jsdom can't resolve custom properties or compute
// contrast, so these only run against a real Playwright page.
// ---------------------------------------------------------------------------

/** Parse an `rgb(r, g, b)` / `rgba(r, g, b, a)` string into channel values. */
export function parseRgb(color: string): [number, number, number] {
  const m = color.match(/rgba?\(([\d.]+),\s*([\d.]+),\s*([\d.]+)/);
  if (!m) throw new Error(`Unparseable colour: ${color}`);
  return [Number(m[1]), Number(m[2]), Number(m[3])];
}

/** WCAG relative luminance (sRGB, 0..1). */
export function relativeLuminance([r, g, b]: [number, number, number]): number {
  const chan = (c: number) => {
    const s = c / 255;
    return s <= 0.03928 ? s / 12.92 : Math.pow((s + 0.055) / 1.055, 2.4);
  };
  const [rl, gl, bl] = [chan(r), chan(g), chan(b)];
  return 0.2126 * rl + 0.7152 * gl + 0.0722 * bl;
}

/** WCAG contrast ratio between two colours, each 4.5:1-testable either order. */
export function contrastRatio(a: string, b: string): number {
  const la = relativeLuminance(parseRgb(a));
  const lb = relativeLuminance(parseRgb(b));
  const [lighter, darker] = la > lb ? [la, lb] : [lb, la];
  return (lighter + 0.05) / (darker + 0.05);
}

/** Type a message and click Send (waits for Send button to be enabled). */
export async function sendChatMessage(page: Page, text: string): Promise<void> {
  await page.fill('[placeholder="Message Seshat..."]', text);
  await page.click('[aria-label="Send message"]');
}

// ---------------------------------------------------------------------------
// Safe-area inset override (FRE-1267)
// ---------------------------------------------------------------------------

/**
 * Force the `--safe-bottom` custom property (globals.css) to a fixed pixel
 * value so a non-zero `env(safe-area-inset-bottom)` can be exercised in a
 * headless Chromium run, where the real env() value is always 0. Call after
 * `page.goto()` — the `!important` root declaration wins the cascade and
 * `var()` resolution updates on the next layout, no reload needed.
 */
export async function overrideSafeAreaBottom(page: Page, px: number): Promise<void> {
  await page.addStyleTag({ content: `:root { --safe-bottom: ${px}px !important; }` });
}
