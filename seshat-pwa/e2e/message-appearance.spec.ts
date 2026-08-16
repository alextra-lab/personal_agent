/**
 * Playwright e2e tests for message appearance (FRE-1264 AC-4, AC-5, AC-6).
 * Real rendered CSS/layout is required — jsdom can't compute box sizes or
 * resolve prose typography, so these ACs can only be proven here.
 */

import { test, expect } from '@playwright/test';
import { TEST_SESSION, stubWebSocket, serverSend, sendChatMessage } from './helpers';

const CHAT_URL = `/c/${TEST_SESSION}`;
const BACKEND = 'http://localhost:9000';

const USER_TEXT = 'What is the capital of France?';
// Deliberately no markdown formatting — react-markdown renders this as a
// single <p>, which keeps the AC-4 style assertions unambiguous.
const ASSISTANT_TEXT = 'The capital of France is a city on the river Seine.';

async function stubHistory(page: import('@playwright/test').Page): Promise<void> {
  const now = new Date().toISOString();
  await page.route(`${BACKEND}/api/v1/sessions/${TEST_SESSION}/messages*`, (route) =>
    route.fulfill({
      json: [
        { role: 'user', content: USER_TEXT, timestamp: now },
        { role: 'assistant', content: ASSISTANT_TEXT, timestamp: now, trace_id: 'trace-1' },
      ],
    }),
  );
  await page.route(`${BACKEND}/api/v1/sessions/${TEST_SESSION}`, (route) =>
    route.fulfill({ status: 404, body: 'not found' }),
  );
  await page.route(`${BACKEND}/chat/stream`, (route) => route.fulfill({ status: 200, body: '' }));
}

test.describe('Message appearance (FRE-1264 AC-4, AC-5)', () => {
  test.use({ viewport: { width: 390, height: 844 } });

  for (const scheme of ['light', 'dark'] as const) {
    test(`AC-4/AC-5 in ${scheme} mode`, async ({ page }) => {
      await page.emulateMedia({ colorScheme: scheme });
      await stubHistory(page);
      await stubWebSocket(page);
      await page.goto(CHAT_URL);

      // The page route wraps StreamingChat in its own <main>, and
      // StreamingChat's scrollable message list is a second, inner <main> —
      // scope to that one specifically so the header (a sibling inside the
      // outer <main>) isn't included in the "no role label" assertions below.
      const main = page.locator('main main');
      const userText = main.getByText(USER_TEXT, { exact: true });
      const assistantText = main.getByText(ASSISTANT_TEXT, { exact: true });
      await userText.waitFor();
      await assistantText.waitFor();

      // AC-5: no role-label chrome left anywhere in the message list.
      await expect(main.getByText('You', { exact: true })).toHaveCount(0);
      await expect(main.getByText('Seshat', { exact: true })).toHaveCount(0);
      await expect(main.getByText('Y', { exact: true })).toHaveCount(0);
      await expect(main.getByText('S', { exact: true })).toHaveCount(0);

      // AC-5: user message — right-aligned bubble, capped at 80% of the
      // scrollable column's width.
      const userBox = await userText.boundingBox();
      const mainBox = await main.boundingBox();
      expect(userBox).not.toBeNull();
      expect(mainBox).not.toBeNull();
      const userRight = userBox!.x + userBox!.width;
      const mainRight = mainBox!.x + mainBox!.width;
      expect(userRight).toBeGreaterThan(mainRight - 40); // hugs the right edge
      expect(userBox!.width).toBeLessThanOrEqual(mainBox!.width * 0.8);

      // AC-5: assistant message — spans (most of) the full column width,
      // clearly wider than the capped user bubble.
      const assistantBox = await assistantText.boundingBox();
      expect(assistantBox).not.toBeNull();
      expect(assistantBox!.width).toBeGreaterThan(userBox!.width);

      // AC-4: assistant prose is serif, >=16px, line-height >=1.5.
      const style = await assistantText.evaluate((el) => {
        const s = getComputedStyle(el);
        return { fontFamily: s.fontFamily, fontSize: parseFloat(s.fontSize), lineHeight: parseFloat(s.lineHeight) };
      });
      expect(style.fontFamily.toLowerCase()).toMatch(/georgia|serif/);
      expect(style.fontSize).toBeGreaterThanOrEqual(16);
      expect(style.lineHeight / style.fontSize).toBeGreaterThanOrEqual(1.5);

      // AC-5: the user bubble is sans-serif (distinct from assistant serif).
      const userFontFamily = await userText.evaluate((el) => getComputedStyle(el).fontFamily);
      expect(userFontFamily.toLowerCase()).not.toMatch(/georgia/);
    });
  }
});

test.describe('Tool call collapse (FRE-1264 AC-6)', () => {
  test.use({ viewport: { width: 390, height: 844 } });

  test('more than one active tool collapses to a summary by default, expands on click', async ({ page }) => {
    await page.route(`${BACKEND}/api/v1/sessions/${TEST_SESSION}/messages*`, (route) =>
      route.fulfill({ json: [] }),
    );
    await page.route(`${BACKEND}/api/v1/sessions/${TEST_SESSION}`, (route) =>
      route.fulfill({ status: 404, body: 'not found' }),
    );
    await page.route(`${BACKEND}/chat/stream`, (route) => route.fulfill({ status: 200, body: '' }));
    const { wsReady } = await stubWebSocket(page);

    await page.goto(CHAT_URL);
    await sendChatMessage(page, 'run two tools');
    const ws = await wsReady;

    serverSend(ws, {
      type: 'TOOL_CALL_START',
      request_id: 'r1',
      session_id: TEST_SESSION,
      seq: 1,
      data: { tool_name: 'read_file' },
    });
    serverSend(ws, {
      type: 'TOOL_CALL_START',
      request_id: 'r2',
      session_id: TEST_SESSION,
      seq: 2,
      data: { tool_name: 'run_python' },
    });

    // Fails if the badges still render expanded by default (AC-6).
    await expect(page.getByText('read_file')).toHaveCount(0);
    await expect(page.getByText('run_python')).toHaveCount(0);
    const toggle = page.getByRole('button', { name: /using 2 tools/i });
    await expect(toggle).toBeVisible();
    await expect(toggle).toHaveAttribute('aria-expanded', 'false');

    await toggle.click();
    await expect(toggle).toHaveAttribute('aria-expanded', 'true');
    await expect(page.getByText('read_file')).toBeVisible();
    await expect(page.getByText('run_python')).toBeVisible();

    serverSend(ws, {
      type: 'TOOL_CALL_END',
      request_id: 'r1',
      session_id: TEST_SESSION,
      seq: 3,
      data: { tool_name: 'read_file', result: 'ok' },
    });
    serverSend(ws, {
      type: 'TOOL_CALL_END',
      request_id: 'r2',
      session_id: TEST_SESSION,
      seq: 4,
      data: { tool_name: 'run_python', result: 'ok' },
    });

    await expect(page.getByRole('button', { name: /used 2 tools/i })).toBeVisible();
  });
});
