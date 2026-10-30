/**
 * Playwright e2e for the read-only settings menu (FRE-1702, AC-1 / AC-2 / AC-5), and for the
 * accordion groups that are closed by default (FRE-1710).
 *
 * Runs against the production bundle (see playwright.config.ts). The backend is mocked:
 * the payload below has the shape of `GET /api/v1/settings`, which the Python tests in
 * tests/personal_agent/service/test_settings_menu*.py pin to the real builder.
 */

import { test, expect, type Page } from '@playwright/test';
import { TEST_SESSION, stubRest, stubWebSocket } from './helpers';

const BACKEND = 'http://localhost:9000';
const SOURCES = ['code default', '.env', 'config file', 'database row', 'session'];

const GROUPS: [string, string][] = [
  ['conversation', 'This conversation'],
  ['saved_choices', 'Saved choices'],
  ['models_per_role', 'Models per role'],
  ['effort_and_modes', 'Effort presets and model modes'],
  ['budgets', 'Budgets and cost'],
  ['turn_runs', 'How a turn runs'],
  ['memory', 'Memory and recall'],
  ['tools', 'Tools and governance'],
  ['grounding', 'Grounding and quality'],
];

function row(name: string, value: unknown, source = 'config file', extra: object = {}) {
  return { name, value, source, overrides: null, clearable: false, note: null, ...extra };
}

function payload(opts: { saved: boolean }) {
  const groups = GROUPS.map(([id, title]) => ({
    id,
    title,
    error: null,
    rows: [row(`${id}.example`, `live-${id}`)],
  }));
  groups[1].rows = [
    opts.saved
      ? row('saved_choice.artifact_builder', 'claude_sonnet', 'database row', {
          overrides: { value: 'gpt-6.1-sol', source: 'config file' },
          clearable: true,
        })
      : row('saved_choice.artifact_builder', 'not set', 'code default'),
    row('saved_choice.tool_iteration_limit', 'not set', 'code default'),
  ];
  groups[3].rows = [
    row('effort.level.deep', { expansion: true, sub_agent_rounds: 20, planner_policy: 'baseline' }),
  ];
  return {
    groups,
    system: {
      id: 'system',
      title: 'System (read-only)',
      error: null,
      rows: [
        row('setting.neo4j_password', 'set', '.env'),
        row('setting.database_url', 'set', '.env'),
        row('setting.consolidation_enabled', true, 'code default'),
      ],
    },
  };
}

async function stubSettings(page: Page, state: { saved: boolean }) {
  await page.route(`${BACKEND}/api/v1/settings*`, (route) =>
    route.fulfill({ json: payload(state) }),
  );
}

/** The accordion header of a group: the button inside the group heading. */
function header(page: Page, title: string) {
  return page.getByRole('heading', { level: 2, name: new RegExp(`^${title}`) }).getByRole('button');
}

test.describe('Settings menu (FRE-1702)', () => {
  test.use({ viewport: { width: 1000, height: 900 } });

  test('AC-1: the nine groups render, each row with a value and a source', async ({ page }) => {
    await stubSettings(page, { saved: false });
    await page.goto('/settings');

    for (const [, title] of GROUPS) {
      await expect(header(page, title)).toHaveAttribute('aria-expanded', 'false');
    }
    await expect(page.getByRole('heading', { level: 2, name: /System/ })).toHaveCount(0);
    await expect(page.getByTestId('setting-row')).toHaveCount(0);

    for (const [, title] of GROUPS) {
      await header(page, title).click();
    }
    const rows = page.getByTestId('setting-row');
    const count = await rows.count();
    expect(count).toBeGreaterThanOrEqual(GROUPS.length);
    for (let i = 0; i < count; i++) {
      const text = (await rows.nth(i).innerText()).trim();
      expect(text.length).toBeGreaterThan(0);
      expect(SOURCES.some((s) => text.toLowerCase().includes(s.toLowerCase()))).toBe(true);
    }
    await expect(page.getByText('live-memory')).toBeVisible();
    await page.screenshot({ path: 'test-results/settings-menu.png', fullPage: true });
  });

  test('AC-1: the System page renders its group; a hidden value reads set', async ({ page }) => {
    await stubSettings(page, { saved: false });
    await page.goto('/settings');
    await page.getByRole('link', { name: 'System' }).click();

    await expect(page).toHaveURL(/\/settings\/system$/);
    await expect(header(page, 'System \\(read-only\\)')).toHaveAttribute('aria-expanded', 'false');
    await header(page, 'System \\(read-only\\)').click();
    const secret = page.getByTestId('setting-row').filter({ hasText: 'setting.neo4j_password' });
    await expect(secret).toContainText('set');
    await expect(secret).toContainText('.env');
    await page.screenshot({ path: 'test-results/settings-system.png', fullPage: true });
  });

  test('AC-2: a saved choice shows beside the default it overrides', async ({ page }) => {
    await stubSettings(page, { saved: true });
    await page.goto('/settings');

    await expect(header(page, 'Saved choices')).toContainText('saved choice');
    await header(page, 'Saved choices').click();
    const saved = page.getByTestId('setting-row').filter({ hasText: 'saved_choice.artifact_builder' });
    await expect(saved).toContainText('claude_sonnet');
    await expect(saved).toContainText('database row');
    await expect(saved).toContainText('overrides');
    await expect(saved).toContainText('gpt-6.1-sol');
  });

  test('AC-5: the only control is Clear; it calls the DELETE and the row resets', async ({ page }) => {
    const state = { saved: true };
    await stubSettings(page, state);
    let deleteUrl = '';
    await page.route(`${BACKEND}/api/v1/preferences/constraint*`, (route) => {
      deleteUrl = route.request().url();
      expect(route.request().method()).toBe('DELETE');
      state.saved = false;
      return route.fulfill({ json: { constraint_name: 'artifact_builder', cleared: true } });
    });
    await page.goto('/settings');

    await header(page, 'Saved choices').click();
    await expect(page.locator('input, select, textarea')).toHaveCount(0);
    const buttons = page.getByRole('button', { name: 'Clear' });
    await expect(buttons).toHaveCount(1);
    await expect(buttons.first()).toHaveText('Clear');

    await buttons.first().click();
    await expect(page.getByRole('button', { name: 'Clear' })).toHaveCount(0);
    expect(deleteUrl).toContain('constraint_name=artifact_builder');
    await expect(
      page.getByTestId('setting-row').filter({ hasText: 'saved_choice.artifact_builder' }),
    ).toContainText('not set');
  });

  test('the drawer links to Settings with the open session', async ({ page }) => {
    await stubRest(page);
    await stubWebSocket(page);
    await stubSettings(page, { saved: false });
    await page.goto(`/c/${TEST_SESSION}`);

    await page.getByLabel('Open session list').click();
    const link = page.getByRole('link', { name: 'Settings' });
    await expect(link).toHaveAttribute('href', `/settings?session=${TEST_SESSION}`);
    await link.click();
    await expect(header(page, 'This conversation')).toBeVisible();
  });
});

test.describe('Settings accordion at phone width (FRE-1710, AC-4)', () => {
  test.use({ viewport: { width: 390, height: 844 } });

  test('all groups closed, then one group open', async ({ page }) => {
    await stubSettings(page, { saved: true });
    await page.goto('/settings');
    await expect(header(page, 'Saved choices')).toBeVisible();
    await page.screenshot({ path: 'test-results/settings-accordion-closed.png', fullPage: true });

    await header(page, 'Saved choices').click();
    await expect(header(page, 'Saved choices')).toHaveAttribute('aria-expanded', 'true');
    await page.waitForTimeout(300); // let the chevron finish turning
    await page.screenshot({ path: 'test-results/settings-accordion-open.png', fullPage: true });
  });
});
