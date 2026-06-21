/**
 * Playwright e2e tests for the PWA chat interface (FRE-400 WS3).
 *
 * All five scenarios mock the backend entirely:
 *   1. Constraint pause → DecisionCard → CONSTRAINT_DECISION → CONSTRAINT_RESOLVED
 *   2. Send → Stop → USER_CANCEL
 *   3. turn_status STATE_DELTA → TurnStatusBar two-lane render
 *   4. RUN_ERROR → ClassifiedErrorCard → Retry re-sends
 *   5. TurnStatusBar remount resilience — artifact → conversation view-switch (FRE-584)
 *
 * No live Seshat server is required. The WebSocket is intercepted via
 * page.routeWebSocket(); REST endpoints via page.route().
 */

import { test, expect } from '@playwright/test';
import {
  TEST_SESSION,
  sendChatMessage,
  serverSend,
  stubRest,
  stubWebSocket,
} from './helpers';

const CHAT_URL = `/c/${TEST_SESSION}`;

// ---------------------------------------------------------------------------
// 1. Constraint pause round-trip (ADR-0076)
// ---------------------------------------------------------------------------

test.describe('Constraint pause round-trip', () => {
  test('CONSTRAINT_PAUSE renders DecisionCard; decision sends CONSTRAINT_DECISION; CONSTRAINT_RESOLVED collapses card', async ({
    page,
  }) => {
    await stubRest(page);
    const { wsReady, received } = await stubWebSocket(page);

    await page.goto(CHAT_URL);
    await page.waitForSelector('[placeholder="Message Seshat..."]');

    // Send a message to open the WebSocket.
    await sendChatMessage(page, 'run a long task');
    const ws = await wsReady;

    // Push a constraint pause from the "server".
    const requestId = 'req-e2e-001';
    serverSend(ws, {
      type: 'CONSTRAINT_PAUSE',
      request_id: requestId,
      session_id: TEST_SESSION,
      seq: 1,
      data: {
        constraint: 'tool_iteration_limit',
        context: 'You have used 10 tool iterations.',
        options: ['continue_10', 'finish_now'],
        default_option: 'finish_now',
        expires_at: new Date(Date.now() + 30_000).toISOString(),
      },
    });

    // DecisionCard must appear.
    await expect(page.getByRole('group')).toBeVisible();
    await expect(page.getByText('You have used 10 tool iterations.')).toBeVisible();
    await expect(page.getByText('Continue (10 more)')).toBeVisible();

    // Click the first option.
    await page.getByText('Continue (10 more)').click();

    // Client must have sent a CONSTRAINT_DECISION with the correct action_id.
    const decisions = received
      .map((m) => { try { return JSON.parse(m) as Record<string, unknown>; } catch { return null; } })
      .filter((m) => m !== null && m.type === 'CONSTRAINT_DECISION');
    expect(decisions).toHaveLength(1);
    expect(decisions[0]).toMatchObject({
      type: 'CONSTRAINT_DECISION',
      request_id: requestId,
      decision: 'continue_10',
    });

    // Push CONSTRAINT_RESOLVED — card must collapse.
    serverSend(ws, {
      type: 'CONSTRAINT_RESOLVED',
      request_id: requestId,
      session_id: TEST_SESSION,
      seq: 2,
      data: {
        constraint: 'tool_iteration_limit',
        action_id: 'continue_10',
        resolution: 'user_choice',
      },
    });

    // DecisionCard (role="group") must disappear.
    // The resolved pill may still show the label text as plain text, so we
    // assert on the group role (the interactive card container) not the label.
    await expect(page.getByRole('group')).not.toBeVisible({ timeout: 3_000 });
  });
});

// ---------------------------------------------------------------------------
// 2. Send → Stop → USER_CANCEL (ADR-0076)
// ---------------------------------------------------------------------------

test.describe('Send → Stop button', () => {
  test('Stop button appears while streaming; clicking it sends USER_CANCEL', async ({ page }) => {
    await stubRest(page);
    const { wsReady, received } = await stubWebSocket(page);

    await page.goto(CHAT_URL);
    await page.waitForSelector('[placeholder="Message Seshat..."]');

    // Before streaming: Send button present, Stop absent.
    await expect(page.getByLabel('Send message')).toBeVisible();
    await expect(page.getByLabel('Stop generating')).not.toBeVisible();

    await sendChatMessage(page, 'stream something');
    const ws = await wsReady;

    // Push a TEXT_DELTA to simulate streaming.
    serverSend(ws, {
      type: 'TEXT_DELTA',
      data: { text: 'Thinking…' },
      session_id: TEST_SESSION,
      seq: 1,
    });

    // Stop button must now be visible.
    await expect(page.getByLabel('Stop generating')).toBeVisible();
    await expect(page.getByLabel('Send message')).not.toBeVisible();

    // Textarea should still be writable (FRE-421).
    await expect(page.locator('[placeholder="Message Seshat..."]')).not.toBeDisabled();

    // Click Stop.
    await page.getByLabel('Stop generating').click();

    // Client must have sent USER_CANCEL.
    const cancels = received
      .map((m) => { try { return JSON.parse(m) as Record<string, unknown>; } catch { return null; } })
      .filter((m) => m !== null && m.type === 'USER_CANCEL');
    expect(cancels).toHaveLength(1);
  });
});

// ---------------------------------------------------------------------------
// 3. turn_status STATE_DELTA → TurnStatusBar (ADR-0076)
// ---------------------------------------------------------------------------

test.describe('TurnStatusBar live update', () => {
  test('STATE_DELTA turn_status renders context usage in the status bar', async ({ page }) => {
    await stubRest(page);
    const { wsReady } = await stubWebSocket(page);

    await page.goto(CHAT_URL);
    await page.waitForSelector('[placeholder="Message Seshat..."]');

    await sendChatMessage(page, 'check status');
    const ws = await wsReady;

    // Push a turn_status STATE_DELTA with known values (ADR-0092 two-lane fields).
    serverSend(ws, {
      type: 'STATE_DELTA',
      data: {
        key: 'turn_status',
        value: {
          context_tokens: 25000,
          context_max: 100000,
          tool_iteration: 3,
          tool_iteration_max: 10,
          turn_cost_usd: 0.05,
          trace_id: 'trace-e2e',
          // ADR-0092 session lane
          session_cost_usd: 0.12,
          session_context_tokens: 25000,
          compaction_count: 0,
          cache_reset_count: 0,
          quality_alert_count: 0,
          quality_alert: null,
        },
      },
      session_id: TEST_SESSION,
      seq: 1,
    });

    // Session lane: "ctx" label and context percentage.
    await expect(page.getByText('ctx')).toBeVisible();
    await expect(page.getByText(/25%/)).toBeVisible();
    // Session lane: cumulative cost.
    await expect(page.getByText('$0.12')).toBeVisible();
    // Engagement lane: tool count.
    await expect(page.getByText(/tools 3\/10/)).toBeVisible();
  });
});

// ---------------------------------------------------------------------------
// 4. RUN_ERROR → ClassifiedErrorCard → Retry (FRE-398)
// ---------------------------------------------------------------------------

test.describe('RUN_ERROR error card', () => {
  test('RUN_ERROR renders ClassifiedErrorCard; Retry re-posts to chat/stream', async ({ page }) => {
    let chatStreamCallCount = 0;
    // stub all routes first, then add the counting override last.
    // Playwright evaluates routes LIFO, so the last-registered handler wins.
    await stubRest(page);
    await page.route('http://localhost:9000/chat/stream', (route) => {
      chatStreamCallCount += 1;
      return route.fulfill({ status: 200, body: '' });
    });

    const { wsReady } = await stubWebSocket(page);

    await page.goto(CHAT_URL);
    await page.waitForSelector('[placeholder="Message Seshat..."]');

    await sendChatMessage(page, 'risky operation');
    const ws = await wsReady;

    // Push a RUN_ERROR from the server.
    serverSend(ws, {
      type: 'RUN_ERROR',
      session_id: TEST_SESSION,
      trace_id: 'trace-err-e2e',
      seq: 1,
      data: {
        category: 'model_server',
        reason: 'The local model server returned HTTP 500.',
        next_step: 'Check that the model server is running.',
        actions: ['retry', 'stop'],
        partial: false,
      },
    });

    // ClassifiedErrorCard must appear with role="alert".
    // Use .filter() to target specifically the error card in case multiple
    // alert elements exist (e.g. from browser or other component state).
    const errorCard = page.getByRole('alert').filter({ hasText: 'Model server error' });
    await expect(errorCard).toBeVisible();
    await expect(page.getByText('Model server error')).toBeVisible();
    await expect(page.getByText('The local model server returned HTTP 500.')).toBeVisible();

    const countBefore = chatStreamCallCount;

    // Click Retry — triggers sendMessage() which POSTs to /chat/stream again.
    await page.getByText('Retry').click();

    // One more POST to /chat/stream must have occurred.
    await expect
      .poll(() => chatStreamCallCount, { timeout: 3_000 })
      .toBeGreaterThan(countBefore);
  });
});

// ---------------------------------------------------------------------------
// 5. TurnStatusBar remount resilience — artifact → conversation (FRE-584)
// ---------------------------------------------------------------------------
// Regression guard for the real bug (trace 0b959afd, 2026-06-17): navigating
// from the conversation to the artifact view and back reset the meter to 0.
// FRE-573 fixed this via localStorage persist (DONE) + seedTurnStatus restore.
// ---------------------------------------------------------------------------

test.describe('TurnStatusBar remount resilience', () => {
  test('session lane and engagement lane survive artifact → conversation view-switch', async ({
    page,
  }) => {
    // Base REST stubs (GET /sessions/{id} → 404 by default).
    await stubRest(page);

    // Override: return session hydration data so seedTurnStatus fires on remount.
    // Registered after stubRest → takes priority (Playwright routes are LIFO).
    await page.route(`http://localhost:9000/api/v1/sessions/${TEST_SESSION}`, (route) =>
      route.fulfill({
        json: {
          id: TEST_SESSION,
          mode: 'local',
          channel: null,
          execution_profile: 'local',
          message_count: 2,
          title: 'Test session',
          created_at: new Date().toISOString(),
          last_active_at: new Date().toISOString(),
          context_tokens: 25000,
          context_max: 100000,
          cost_usd: 0.47,
        },
      }),
    );

    // Stub the artifacts index endpoint (body.items shape per listArtifacts()).
    await page.route('http://localhost:9000/api/v1/artifacts*', (route) =>
      route.fulfill({ json: { items: [] } }),
    );

    const { wsReady } = await stubWebSocket(page);

    await page.goto(CHAT_URL);
    await page.waitForSelector('[placeholder="Message Seshat..."]');

    // Wait for getSession hydration to complete before pushing any WS events.
    // This guarantees seedTurnStatus(tool_iteration=0) has already fired,
    // eliminating the race where a late seed overwrites a live STATE_DELTA.
    await expect(page.getByText('$0.47')).toBeVisible({ timeout: 5_000 });

    // Start a turn and open the WebSocket.
    await sendChatMessage(page, 'run task with tools');
    const ws = await wsReady;
    await expect(page.getByLabel('Stop generating')).toBeVisible({ timeout: 3_000 });

    // Push a turn_status with tool_iteration > 0 and full session-lane fields.
    serverSend(ws, {
      type: 'STATE_DELTA',
      data: {
        key: 'turn_status',
        value: {
          context_tokens: 25000,
          context_max: 100000,
          tool_iteration: 4,
          tool_iteration_max: 6,
          turn_cost_usd: 0.18,
          trace_id: 'trace-remount-e2e',
          session_cost_usd: 0.47,
          session_context_tokens: 25000,
          compaction_count: 0,
          cache_reset_count: 0,
          quality_alert_count: 0,
          quality_alert: null,
        },
      },
      session_id: TEST_SESSION,
      seq: 1,
    });

    // Verify the two-lane bar is showing live values before navigation.
    await expect(page.getByText('$0.47')).toBeVisible();
    await expect(page.getByText(/tools 4\/6/)).toBeVisible();

    // Send DONE — useSSEStream writes { tool_iteration: 4, tool_iteration_max: 6 }
    // to localStorage synchronously inside the DONE handler.
    serverSend(ws, {
      type: 'DONE',
      session_id: TEST_SESSION,
      seq: 2,
      data: {},
    });
    // Wait for isStreaming=false (Send button returns) — DONE handler has fired
    // and localStorage has been written before React commits this re-render.
    await expect(page.getByLabel('Send message')).toBeVisible({ timeout: 3_000 });

    // Navigate to the artifacts view — unmounts StreamingChat and TurnStatusBar.
    await page.goto('/artifacts');
    await expect(page.getByRole('heading', { name: 'Artifacts' })).toBeVisible();

    // Navigate back — remounts StreamingChat; getSession hydration + localStorage
    // restore run inside the mount useEffect (StreamingChat.tsx seedTurnStatus).
    await page.goto(CHAT_URL);
    await page.waitForSelector('[placeholder="Message Seshat..."]');

    // Session lane must be restored from FRE-426 hydration, not reset to 0.
    await expect(page.getByText('$0.47')).toBeVisible();
    await expect(page.getByText(/25%/)).toBeVisible();

    // Engagement lane must be restored from localStorage, not show 0/6.
    await expect(page.getByText(/tools 4\/6/)).toBeVisible();
    await expect(page.getByText(/tools 0\/6/)).not.toBeVisible({ timeout: 1_000 });
  });
});
