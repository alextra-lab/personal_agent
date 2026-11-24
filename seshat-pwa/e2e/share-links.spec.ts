/**
 * Playwright e2e for share links in the PWA (FRE-1784 AC-1 to AC-5; ADR-0153 Amendment 1 L9, A1.2).
 *
 * Runs against the production bundle (see playwright.config.ts). The backend is mocked with
 * page.route(), in the shapes of src/personal_agent/service/share_links_router.py and the
 * `can_share_link` field of src/personal_agent/service/artifacts_router.py.
 */

import { test, expect, type Page, type Route } from '@playwright/test';
import { TEST_SESSION, stubWebSocket } from './helpers';

const BACKEND = 'http://localhost:9000';
const ARTIFACTS_HOST = 'https://artifacts.example.com';
const EXPOSURE =
  'Anyone who gets the link can open the artifact, until the link expires or the owner revokes it. A forwarded link works for anyone.';

const HTML_ID = '11111111-1111-4111-8111-111111111111';
const PDF_ID = '22222222-2222-4222-8222-222222222222';
const NOTE_ID = '33333333-3333-4333-8333-333333333333';
const NOFLAG_ID = '44444444-4444-4444-8444-444444444444';

function summary(id: string, title: string, contentType: string, canShare: boolean) {
  return {
    artifact_id: id,
    public_url: `${ARTIFACTS_HOST}/${id}`,
    slug: title.toLowerCase().replace(/\s+/g, '-'),
    title,
    summary: null,
    content_type: contentType,
    size_bytes: 1024,
    tags: [],
    created_at: new Date().toISOString(),
    can_share_link: canShare,
  };
}

const ITEMS = {
  [HTML_ID]: summary(HTML_ID, 'HTML report', 'text/html; charset=utf-8', true),
  [PDF_ID]: summary(PDF_ID, 'Uploaded PDF', 'application/pdf', true),
  [NOTE_ID]: summary(NOTE_ID, 'A note', 'text/markdown; charset=utf-8', false),
  [NOFLAG_ID]: summary(NOFLAG_ID, 'Not shareable', 'text/html; charset=utf-8', false),
};

interface LinkRow {
  link_id: string;
  artifact_id: string;
  artifact_title: string | null;
  created_at: string;
  expires_at: string;
  view_count: number;
  last_viewed_at: string | null;
}

/** A stateful mock of the three share-link endpoints. */
async function stubShareLinks(page: Page) {
  const state = {
    links: [] as LinkRow[],
    posts: [] as unknown[],
    deletes: [] as string[],
    urls: [] as string[],
  };
  await page.route(/\/api\/v1\/artifacts\/[^/]+\/share-links$/, async (route: Route) => {
    const req = route.request();
    if (req.method() !== 'POST') return route.fallback();
    state.posts.push(req.postDataJSON());
    const artifactId = new URL(req.url()).pathname.split('/')[4];
    const n = state.links.length + state.deletes.length + 1;
    const days = (req.postDataJSON() as { expires_in_days: number }).expires_in_days;
    const expiresAt = new Date(Date.now() + days * 86_400_000).toISOString();
    const url = `https://share.example.com/s/token-${n}`;
    state.urls.push(url);
    state.links.unshift({
      link_id: `link-${n}`,
      artifact_id: artifactId,
      artifact_title: ITEMS[artifactId as keyof typeof ITEMS]?.title ?? null,
      created_at: new Date().toISOString(),
      expires_at: expiresAt,
      view_count: 0,
      last_viewed_at: null,
    });
    return route.fulfill({ status: 201, json: { link_id: `link-${n}`, url, expires_at: expiresAt } });
  });
  await page.route(`${BACKEND}/api/v1/share-links`, (route) =>
    route.fulfill({ json: { items: state.links } }),
  );
  await page.route(/\/api\/v1\/share-links\/[^/]+$/, (route) => {
    const linkId = new URL(route.request().url()).pathname.split('/').pop() ?? '';
    state.deletes.push(linkId);
    state.links = state.links.filter((l) => l.link_id !== linkId);
    return route.fulfill({ status: 204, body: '' });
  });
  return state;
}

/** Stub the artifact metadata and list endpoints from ITEMS. */
async function stubArtifacts(page: Page) {
  await page.route(/\/api\/v1\/artifacts\/[0-9a-f-]{36}$/, (route) => {
    const id = new URL(route.request().url()).pathname.split('/').pop() ?? '';
    const item = ITEMS[id as keyof typeof ITEMS];
    return item ? route.fulfill({ json: item }) : route.fulfill({ status: 404, body: '' });
  });
  await page.route(/\/api\/v1\/artifacts\?/, (route) =>
    route.fulfill({ json: { items: [ITEMS[HTML_ID], ITEMS[NOFLAG_ID]] } }),
  );
  await page.route(/\/api\/v1\/telemetry\//, (route) => route.fulfill({ status: 204, body: '' }));
}

/** Open a chat whose history holds one assistant message with all four artifact links. */
async function openChatWithCards(page: Page) {
  const now = new Date().toISOString();
  const content = Object.values(ITEMS)
    .map((item) => `- [${item.title}](${ARTIFACTS_HOST}/${item.artifact_id})`)
    .join('\n');
  await page.route(`${BACKEND}/api/v1/sessions/${TEST_SESSION}/messages*`, (route) =>
    route.fulfill({
      json: [
        { role: 'user', content: 'Show my items', timestamp: now },
        { role: 'assistant', content, timestamp: now, trace_id: 'trace-share-1' },
      ],
    }),
  );
  await page.route(`${BACKEND}/api/v1/sessions/${TEST_SESSION}`, (route) =>
    route.fulfill({ status: 404, body: 'not found' }),
  );
  await stubWebSocket(page);
  await page.goto(`/c/${TEST_SESSION}`);
}

/** The inline card for one title: the smallest div holding the title and its Expand button. */
function card(page: Page, title: string) {
  return page
    .locator('div')
    .filter({ hasText: title })
    .filter({ has: page.getByRole('button', { name: 'Expand' }) })
    .last();
}

/** Make the Web Share API present (recording each call) or absent, before the page loads. */
async function setWebShare(page: Page, present: boolean) {
  await page.addInitScript((isPresent: boolean) => {
    const w = window as unknown as { __shareCalls: ShareData[] };
    w.__shareCalls = [];
    Object.defineProperty(Navigator.prototype, 'share', {
      configurable: true,
      value: isPresent
        ? async (data: ShareData) => {
            w.__shareCalls.push(data);
          }
        : undefined,
    });
  }, present);
}

test.describe('Share links (FRE-1784)', () => {
  // No service worker: the reload in AC-5 must go to the mocked network, not a cache.
  test.use({ viewport: { width: 390, height: 844 }, serviceWorkers: 'block' });

  test('AC-1: Share appears only where can_share_link is true', async ({ page }) => {
    await stubArtifacts(page);
    await openChatWithCards(page);

    for (const title of ['HTML report', 'Uploaded PDF', 'A note', 'Not shareable']) {
      await expect(card(page, title).getByRole('button', { name: 'Expand' })).toBeVisible();
    }
    await expect(card(page, 'HTML report').getByRole('button', { name: 'Share' })).toBeVisible();
    await expect(card(page, 'Uploaded PDF').getByRole('button', { name: 'Share' })).toBeVisible();
    await expect(card(page, 'A note').getByRole('button', { name: 'Share' })).toHaveCount(0);
    await expect(card(page, 'Not shareable').getByRole('button', { name: 'Share' })).toHaveCount(0);
    await expect(page.getByRole('button', { name: 'Share' })).toHaveCount(2);
  });

  test('AC-2 and AC-3: the sentence shows first, one POST of 7 days, then Send shares the URL', async ({
    page,
  }) => {
    await setWebShare(page, true);
    await stubArtifacts(page);
    const state = await stubShareLinks(page);
    await openChatWithCards(page);

    await card(page, 'HTML report').getByRole('button', { name: 'Share' }).click();
    const sheet = page.getByRole('dialog', { name: 'Share HTML report' });
    await expect(sheet.getByText(EXPOSURE, { exact: true })).toBeVisible();
    await expect(sheet.getByLabel('7 days')).toBeChecked();
    expect(state.posts).toHaveLength(0);

    await sheet.getByRole('button', { name: 'Create link' }).click();
    const link = sheet.getByRole('textbox', { name: 'Share link' });
    await expect(link).toHaveValue(state.urls[0]);
    expect(state.posts).toEqual([{ expires_in_days: 7 }]);

    // Send is a second tap.
    expect(await page.evaluate(() => (window as unknown as { __shareCalls: ShareData[] }).__shareCalls)).toEqual([]);
    await sheet.getByRole('button', { name: 'Send' }).click();
    await expect
      .poll(() => page.evaluate(() => (window as unknown as { __shareCalls: ShareData[] }).__shareCalls))
      .toEqual([{ url: state.urls[0] }]);
    await expect(sheet.getByRole('button', { name: 'Copy link' })).toHaveCount(0);
  });

  test('AC-2: without the Web Share API, Copy link writes the URL to the clipboard', async ({
    page,
    context,
  }) => {
    await context.grantPermissions(['clipboard-read', 'clipboard-write'], {
      origin: 'http://localhost:3100',
    });
    await setWebShare(page, false);
    await stubArtifacts(page);
    const state = await stubShareLinks(page);
    await openChatWithCards(page);

    await card(page, 'Uploaded PDF').getByRole('button', { name: 'Share' }).click();
    const sheet = page.getByRole('dialog', { name: 'Share Uploaded PDF' });
    await sheet.getByRole('button', { name: 'Create link' }).click();
    await expect(sheet.getByRole('textbox', { name: 'Share link' })).toHaveValue(state.urls[0]);
    await expect(sheet.getByRole('button', { name: 'Send' })).toHaveCount(0);

    await sheet.getByRole('button', { name: 'Copy link' }).click();
    await expect(sheet.getByText('Copied')).toBeVisible();
    expect(await page.evaluate(() => navigator.clipboard.readText())).toBe(state.urls[0]);
  });

  for (const [status, detail] of [
    [422, 'this artifact loads files from the artifact host that a link cannot reach'],
    [502, 'Bad Gateway'],
  ] as const) {
    test(`AC-4: a ${status} shows the API text and no link`, async ({ page }) => {
      await stubArtifacts(page);
      let posts = 0;
      await page.route(/\/api\/v1\/artifacts\/[^/]+\/share-links$/, (route) => {
        posts += 1;
        return route.fulfill({ status, json: { detail } });
      });
      await openChatWithCards(page);

      await card(page, 'HTML report').getByRole('button', { name: 'Share' }).click();
      const sheet = page.getByRole('dialog', { name: 'Share HTML report' });
      await sheet.getByRole('button', { name: 'Create link' }).click();
      await expect(sheet.getByRole('alert')).toHaveText(detail);
      expect(posts).toBe(1);
      await expect(sheet.getByRole('textbox')).toHaveCount(0);
      await expect(sheet.getByRole('button', { name: 'Send' })).toHaveCount(0);
      await expect(sheet.getByRole('button', { name: 'Copy link' })).toHaveCount(0);
    });
  }

  test('AC-5: the list follows the API, and Revoke removes a link', async ({ page }) => {
    await stubArtifacts(page);
    const state = await stubShareLinks(page);
    await page.goto('/artifacts');

    for (let i = 0; i < 2; i += 1) {
      await page.getByRole('button', { name: 'Share' }).click();
      const sheet = page.getByRole('dialog', { name: 'Share HTML report' });
      await sheet.getByRole('button', { name: 'Create link' }).click();
      await expect(sheet.getByRole('textbox', { name: 'Share link' })).toHaveValue(state.urls[i]);
      await sheet.getByRole('button', { name: 'Close share sheet' }).click();
      await expect(sheet).toHaveCount(0);
    }
    expect(state.posts).toHaveLength(2);

    // The URL holds the token: after the sheet closes, it is not in the page or in storage.
    const kept = await page.evaluate(() => [
      document.documentElement.outerHTML,
      JSON.stringify({ ...localStorage }),
      JSON.stringify({ ...sessionStorage }),
    ]);
    for (const text of kept) {
      expect(text).not.toContain('token-');
    }

    await page.getByRole('link', { name: 'Shared links' }).click();
    await expect(page).toHaveURL(/\/shared-links$/);
    const rows = page.getByRole('listitem');
    await expect(rows).toHaveCount(2);
    for (const row of await rows.all()) {
      await expect(row).toContainText('HTML report');
      await expect(row).toContainText('Expires');
      await expect(row).toContainText('0 views');
    }

    await rows.first().getByRole('button', { name: 'Revoke' }).click();
    await expect(rows).toHaveCount(1);
    expect(state.deletes).toEqual(['link-2']);

    await page.reload();
    await expect(page.getByRole('listitem')).toHaveCount(1);
    expect(state.links.map((l) => l.link_id)).toEqual(['link-1']);
    expect(state.deletes).toHaveLength(1);
  });
});
