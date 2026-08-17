/**
 * Playwright e2e tests for FRE-1265's theme-token extension — the contrast
 * check named in the ticket's AC-2 ("Playwright, emulateMedia both ways,
 * contrast check per FRE-1264's AC-7 pattern"), applied to the newly
 * converted surfaces that are reachable via the existing WS/REST mocking
 * helpers.
 *
 * MermaidBlock's chrome (header, buttons, borders) is checked below; its
 * rendered diagram canvas is a deliberate fixed-dark island exempt from
 * theming (same precedent as MarkdownContent's CodeBlock) so it's excluded
 * from the contrast scan rather than asserted against.
 */

import { test, expect } from '@playwright/test';
import {
  TEST_SESSION,
  sendChatMessage,
  serverSend,
  stubRest,
  stubWebSocket,
  contrastRatio,
} from './helpers';

const CHAT_URL = `/c/${TEST_SESSION}`;

/**
 * Foreground-vs-background contrast for every visible text node inside
 * `locator`. Backgrounds are alpha-composited against every ancestor layer
 * down to the nearest fully-opaque one — several converted surfaces use
 * translucent tokens by design (bg-*-900/40 chips, bg-bg/80 blurred headers),
 * and comparing text against a raw un-composited rgba() understates the
 * actual on-screen contrast. Elements marked aria-hidden are skipped —
 * decorative content (a "|" divider) carries no text-contrast obligation.
 */
async function assertTextContrast(
  locator: import('@playwright/test').Locator,
  minRatio = 4.5,
): Promise<void> {
  const handle = await locator.elementHandle();
  if (!handle) throw new Error('locator did not resolve to an element');
  const results = await handle.evaluate((root: Element) => {
    function parseRgba(color: string): [number, number, number, number] {
      const m = color.match(/rgba?\(([\d.]+),\s*([\d.]+),\s*([\d.]+)(?:,\s*([\d.]+))?\)/);
      if (!m) return [0, 0, 0, 0];
      return [Number(m[1]), Number(m[2]), Number(m[3]), m[4] !== undefined ? Number(m[4]) : 1];
    }

    /** Composite every background layer from the given element up to the root, outermost first. */
    function compositedBackground(from: Element): string {
      const layers: [number, number, number, number][] = [];
      let el: Element | null = from;
      while (el) {
        const [r, g, b, a] = parseRgba(getComputedStyle(el).backgroundColor);
        if (a > 0) layers.push([r, g, b, a]);
        if (a >= 1) break; // fully opaque — nothing further back can show through
        el = el.parentElement;
      }
      layers.reverse(); // outermost (or the opaque base) first
      let [cr, cg, cb] = layers[0] ? layers[0].slice(0, 3) as [number, number, number] : [255, 255, 255];
      for (const [r, g, b, a] of layers.slice(1)) {
        cr = r * a + cr * (1 - a);
        cg = g * a + cg * (1 - a);
        cb = b * a + cb * (1 - a);
      }
      return `rgb(${cr}, ${cg}, ${cb})`;
    }

    const out: { text: string; fg: string; bg: string }[] = [];
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT);
    let node: Element | null = root;
    while (node) {
      const hasOwnText = Array.from(node.childNodes).some(
        (n) => n.nodeType === Node.TEXT_NODE && (n.textContent ?? '').trim().length > 0,
      );
      const hidden = node.closest('[aria-hidden="true"]') !== null;
      if (hasOwnText && !hidden) {
        const bg = compositedBackground(node);
        // Foreground text color may itself be translucent (e.g. text-ink-muted/70)
        // — composite it over bg (always fully opaque) before comparing, or a
        // translucent fg reads as its full-strength, never-actually-rendered
        // color and silently passes a contrast check it should fail.
        const [fr, fg_, fb, fa] = parseRgba(getComputedStyle(node).color);
        const [br, bgG, bb] = parseRgba(bg);
        const fg =
          fa >= 1
            ? `rgb(${fr}, ${fg_}, ${fb})`
            : `rgb(${fr * fa + br * (1 - fa)}, ${fg_ * fa + bgG * (1 - fa)}, ${fb * fa + bb * (1 - fa)})`;
        out.push({ text: (node.textContent ?? '').trim().slice(0, 40), fg, bg });
      }
      node = walker.nextNode() as Element | null;
    }
    return out;
  });

  for (const { text, fg, bg } of results) {
    expect(contrastRatio(fg, bg), `"${text}" (${fg} on ${bg})`).toBeGreaterThanOrEqual(minRatio);
  }
}

for (const scheme of ['light', 'dark'] as const) {
  test.describe(`FRE-1265 converted surfaces — ${scheme} mode`, () => {
    test.use({ viewport: { width: 390, height: 844 }, colorScheme: scheme });

    test('ApprovalModal is legible', async ({ page }) => {
      await stubRest(page);
      const { wsReady } = await stubWebSocket(page);
      await page.goto(CHAT_URL);
      await page.waitForSelector('[placeholder="Message Seshat..."]');
      await sendChatMessage(page, 'do something risky');
      const ws = await wsReady;

      serverSend(ws, {
        type: 'tool_approval_request',
        request_id: 'req-theme-001',
        trace_id: 'trace-theme-001',
        tool: 'run_shell',
        args: { command: 'rm -rf /tmp/x' },
        risk_level: 'high',
        reason: 'This command modifies the filesystem.',
        expires_at: new Date(Date.now() + 30_000).toISOString(),
        session_id: TEST_SESSION,
        seq: 1,
      });

      const modal = page.getByRole('dialog', { name: /Tool approval required/ });
      await expect(modal).toBeVisible();
      await assertTextContrast(modal);
    });

    test('DecisionCard is legible', async ({ page }) => {
      await stubRest(page);
      const { wsReady } = await stubWebSocket(page);
      await page.goto(CHAT_URL);
      await page.waitForSelector('[placeholder="Message Seshat..."]');
      await sendChatMessage(page, 'run a long task');
      const ws = await wsReady;

      serverSend(ws, {
        type: 'CONSTRAINT_PAUSE',
        request_id: 'req-theme-002',
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

      const card = page.getByRole('group');
      await expect(card).toBeVisible();
      await assertTextContrast(card);
    });

    test('ClassifiedErrorCard is legible', async ({ page }) => {
      await stubRest(page);
      const { wsReady } = await stubWebSocket(page);
      await page.goto(CHAT_URL);
      await page.waitForSelector('[placeholder="Message Seshat..."]');
      await sendChatMessage(page, 'risky operation');
      const ws = await wsReady;

      serverSend(ws, {
        type: 'RUN_ERROR',
        session_id: TEST_SESSION,
        trace_id: 'trace-theme-003',
        seq: 1,
        data: {
          category: 'model_server',
          reason: 'The local model server returned HTTP 500.',
          next_step: 'Check that the model server is running.',
          actions: ['retry', 'stop'],
          partial: false,
        },
      });

      const card = page.getByRole('alert').filter({ hasText: 'Model server error' });
      await expect(card).toBeVisible();
      await assertTextContrast(card);
    });

    test('PhaseIndicator is legible in running, completed, and error states', async ({ page }) => {
      await stubRest(page);
      const { wsReady } = await stubWebSocket(page);
      await page.goto(CHAT_URL);
      await page.waitForSelector('[placeholder="Message Seshat..."]');
      await sendChatMessage(page, 'build something');
      const ws = await wsReady;

      serverSend(ws, {
        type: 'PHASE_START',
        session_id: TEST_SESSION,
        seq: 1,
        data: {
          phase: 'planning',
          phase_id: 'phase-running',
          started_at: new Date().toISOString(),
          detail: 'Thinking',
          parent_id: null,
        },
      });
      serverSend(ws, {
        type: 'PHASE_START',
        session_id: TEST_SESSION,
        seq: 2,
        data: {
          phase: 'synthesis',
          phase_id: 'phase-to-complete',
          started_at: new Date().toISOString(),
          detail: 'Writing',
          parent_id: null,
        },
      });
      serverSend(ws, {
        type: 'PHASE_START',
        session_id: TEST_SESSION,
        seq: 3,
        data: {
          phase: 'artifact_build',
          phase_id: 'phase-to-error',
          started_at: new Date().toISOString(),
          detail: 'Building',
          parent_id: null,
        },
      });
      serverSend(ws, {
        type: 'PHASE_END',
        session_id: TEST_SESSION,
        seq: 4,
        data: { phase: 'synthesis', phase_id: 'phase-to-complete', parent_id: null, ok: true },
      });
      serverSend(ws, {
        type: 'PHASE_END',
        session_id: TEST_SESSION,
        seq: 5,
        data: { phase: 'artifact_build', phase_id: 'phase-to-error', parent_id: null, ok: false },
      });

      const indicator = page.getByTestId('phase-phase-running');
      await expect(indicator).toBeVisible();
      const container = page.locator('[data-testid^="phase-phase-"]').first().locator('..');
      await assertTextContrast(container);
    });

    test('ModelPicker is legible, closed and open', async ({ page }) => {
      await stubRest(page);
      await page.route(
        `http://localhost:9000/api/v1/sessions/${TEST_SESSION}/config`,
        (route) =>
          route.fulfill({
            json: {
              session_id: TEST_SESSION,
              roles: {
                primary: {
                  open: true,
                  resolved: 'local-qwen',
                  provenance: 'server-hydrated',
                  candidates: [
                    {
                      key: 'local-qwen',
                      id: 'local-qwen',
                      provider: 'local',
                      placement: 'local',
                      kind: 'chat',
                      status: 'available',
                      summary: 'Local model',
                      context_length: 32000,
                      max_tokens: 4096,
                      supports_vision: false,
                      supports_pdf_document: false,
                      input_cost_per_token: null,
                      output_cost_per_token: null,
                    },
                    {
                      key: 'cloud-opus',
                      id: 'cloud-opus',
                      provider: 'anthropic',
                      placement: 'cloud',
                      kind: 'chat',
                      status: 'available',
                      summary: 'Cloud model',
                      context_length: 200000,
                      max_tokens: 8192,
                      supports_vision: true,
                      supports_pdf_document: true,
                      input_cost_per_token: 0.000003,
                      output_cost_per_token: 0.000015,
                    },
                  ],
                },
              },
              providers: [],
            },
          }),
      );
      await stubWebSocket(page);
      await page.goto(CHAT_URL);
      const trigger = page.getByLabel('Choose model');
      await expect(trigger).toBeVisible();
      await assertTextContrast(trigger);

      await trigger.click();
      const listbox = page.getByRole('listbox');
      await expect(listbox).toBeVisible();
      await assertTextContrast(listbox);
    });

    test('ArtifactsIndex is legible', async ({ page }) => {
      await stubRest(page);
      await page.route('http://localhost:9000/api/v1/artifacts*', (route) =>
        route.fulfill({
          json: {
            items: [
              {
                artifact_id: 'art-1',
                slug: 'art-1',
                title: 'Sample artifact',
                summary: 'A short summary of the artifact.',
                content_type: 'text/html; charset=utf-8',
                public_url: 'https://artifacts.example.com/art-1',
                created_at: new Date().toISOString(),
              },
            ],
          },
        }),
      );
      await stubWebSocket(page);
      await page.goto('/artifacts');
      const heading = page.getByRole('heading', { name: 'Artifacts' });
      await expect(heading).toBeVisible();
      await assertTextContrast(page.locator('body'));
    });

    test('ObserveView is legible', async ({ page }) => {
      await stubRest(page);
      await page.route('http://localhost:9000/api/v1/config', (route) =>
        route.fulfill({
          json: {
            roles: {
              primary: { open: true, resolved: 'local-qwen', provenance: 'default' },
              embedding: { open: false, resolved: 'ovh-embed', provenance: 'default' },
            },
            providers: [
              { key: 'local', placement: 'local', available: true, summary: 'Local runtime', max_concurrency: 2 },
              { key: 'anthropic', placement: 'cloud', available: false, summary: 'Cloud provider', max_concurrency: 4 },
            ],
          },
        }),
      );
      await stubWebSocket(page);
      await page.goto('/observe');
      const heading = page.getByRole('heading', { name: 'Observe' });
      await expect(heading).toBeVisible();
      await assertTextContrast(page.locator('body'));
    });

    test('TurnRating is legible', async ({ page }) => {
      await stubRest(page);
      const { wsReady } = await stubWebSocket(page);
      await page.goto(CHAT_URL);
      await page.waitForSelector('[placeholder="Message Seshat..."]');
      await sendChatMessage(page, 'say something');
      const ws = await wsReady;

      serverSend(ws, { type: 'TEXT_DELTA', session_id: TEST_SESSION, seq: 1, data: { text: 'Here you go.' } });
      serverSend(ws, {
        type: 'DONE',
        session_id: TEST_SESSION,
        trace_id: 'trace-theme-rating',
        seq: 2,
        data: {},
      });

      const rating = page.getByRole('group', { name: 'Rate this response' });
      await expect(rating).toBeVisible();
      await assertTextContrast(rating);
    });

    test('LocationConsent is legible', async ({ page }) => {
      await stubRest(page);
      await page.route('http://localhost:9000/api/v1/preferences/location', (route) =>
        route.fulfill({ json: { feature_enabled: true, location_consent_enabled: false } }),
      );
      await stubWebSocket(page);
      await page.goto(CHAT_URL);
      await page.getByLabel('Open session list').click();
      const toggle = page.getByLabel('Share location with Seshat');
      await expect(toggle).toBeVisible();
      // The toggle row + its explanatory paragraph share one parent <div>.
      // .last() (not .first()) — filter() matches every ancestor div up to
      // <body>, listed outermost-first in document order; .last() is the
      // innermost/smallest one, not a broader ancestor sharing the sidebar
      // with SessionList's own (unrelated, unstubbed-in-this-test) content.
      const row = page.locator('div').filter({ has: toggle }).last();
      await assertTextContrast(row);
    });

    test('ArtifactCard, ArtifactViewer, and ArtifactExportMenu are legible', async ({ page }) => {
      await stubRest(page);
      const artifactId = '11111111-1111-4111-8111-111111111111';
      await page.route(
        `http://localhost:9000/api/v1/artifacts/${artifactId}`,
        (route) =>
          route.fulfill({
            json: {
              artifact_id: artifactId,
              public_url: `https://artifacts.example.com/${artifactId}`,
              slug: 'sample',
              title: 'Sample artifact',
              summary: 'A short summary of the artifact.',
              content_type: 'text/html; charset=utf-8',
              size_bytes: 1024,
              tags: [],
              created_at: new Date().toISOString(),
            },
          }),
      );
      const { wsReady } = await stubWebSocket(page);
      await page.goto(CHAT_URL);
      await page.waitForSelector('[placeholder="Message Seshat..."]');
      await sendChatMessage(page, 'make me an artifact');
      const ws = await wsReady;

      serverSend(ws, {
        type: 'TEXT_DELTA',
        session_id: TEST_SESSION,
        seq: 1,
        data: { text: `Here: https://artifacts.example.com/${artifactId}` },
      });
      serverSend(ws, { type: 'DONE', session_id: TEST_SESSION, trace_id: 'trace-theme-art', seq: 2, data: {} });

      const expandBtn = page.getByText('Expand', { exact: true });
      await expect(expandBtn).toBeVisible();
      // The card's own container — smallest div wrapping both the title and the Expand button.
      const card = page.locator('div').filter({ hasText: 'Sample artifact' }).filter({ has: expandBtn }).last();
      await assertTextContrast(card);

      await expandBtn.click();
      const viewer = page.getByRole('dialog', { name: 'Sample artifact' });
      await expect(viewer).toBeVisible();
      await assertTextContrast(viewer);

      await page.getByLabel('Export artifact').click();
      const menu = page.getByRole('menu');
      await expect(menu).toBeVisible();
      await assertTextContrast(menu);
    });

    test('MermaidBlock chrome is legible', async ({ page }) => {
      await stubRest(page);
      const { wsReady } = await stubWebSocket(page);
      await page.goto(CHAT_URL);
      await page.waitForSelector('[placeholder="Message Seshat..."]');
      await sendChatMessage(page, 'draw a diagram');
      const ws = await wsReady;

      serverSend(ws, {
        type: 'TEXT_DELTA',
        session_id: TEST_SESSION,
        seq: 1,
        data: { text: '```mermaid\nflowchart TD\n  A --> B\n```' },
      });
      serverSend(ws, { type: 'DONE', session_id: TEST_SESSION, trace_id: 'trace-theme-mmd', seq: 2, data: {} });

      // The header row only — "figure" label + (once mermaid's dynamic
      // import resolves) the svg/png/copy/view-source buttons. The rendered
      // diagram canvas below the header is a deliberate fixed-dark island
      // (mermaid's own hardcoded theme) and is out of scope for this scan.
      const figureLabel = page.getByText('figure', { exact: true });
      await expect(figureLabel).toBeVisible({ timeout: 10_000 });
      const header = figureLabel.locator('xpath=ancestor::div[2]');
      await assertTextContrast(header);
    });
  });
}
