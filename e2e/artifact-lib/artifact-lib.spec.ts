/**
 * FRE-531 — E2E verification: curated `/lib/` render under the artifact CSP +
 * offline export (ADR-0089 Addendum A7). Runs on Chromium and WebKit.
 *
 * Fixtures are produced by `scripts/build_e2e_artifact_fixtures.py` into
 * `.fixtures/` (real KaTeX 0.16.11 + Chart.js 4.4.7 bytes, SRI-pinned). Each
 * scenario asserts **zero CSP violations** plus *semantic* render fidelity — a
 * `.katex` node / non-blank canvas alone can pass on a broken render, so we also
 * check the KaTeX MathML annotation echoes the source TeX and the live Chart.js
 * instance holds the seeded dataset.
 *
 *   A — hosted render under the exact CSP directive set (drawer/standalone serve).
 *   B — offline export: the inline `export_artifact_html` output via file://,
 *       all network blocked.
 *   C — paged.js eval-gate (record-only): does paged.js run under the eval-free
 *       CSP without a script/eval CSP violation? The verdict is asserted so a
 *       regression trips CI; the manifest `eval_gated` flag is NOT mutated here.
 */
import { test, expect, type Page } from '@playwright/test';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';
import { startCspServer, type CspServer } from './csp-server';

const FIXTURES = join(__dirname, '.fixtures');

interface BuildManifest {
  artifact: string;
  standalone: string;
  pagedjs: string;
  katex_formula_tex: string;
  chart_data: number[];
}

const manifest: BuildManifest = JSON.parse(
  readFileSync(join(FIXTURES, 'build-manifest.json'), 'utf-8'),
);

interface CspViolation {
  directive: string;
  blockedURI: string;
}

/** Install a CSP-violation collector before any document script runs. */
async function collectCspViolations(page: Page): Promise<void> {
  await page.addInitScript(() => {
    (window as unknown as { __csp: CspViolation[] }).__csp = [];
    document.addEventListener('securitypolicyviolation', (e) => {
      (window as unknown as { __csp: CspViolation[] }).__csp.push({
        directive: e.violatedDirective,
        blockedURI: e.blockedURI,
      });
    });
  });
}

function readViolations(page: Page): Promise<CspViolation[]> {
  return page.evaluate(() => (window as unknown as { __csp: CspViolation[] }).__csp);
}

/** Assert the KaTeX formula + Chart.js chart rendered correctly (semantic, not just present). */
async function assertCuratedRender(page: Page): Promise<void> {
  // KaTeX produced its render tree.
  await expect(page.locator('#formula .katex-html')).toBeVisible();
  expect(await page.locator('#formula .katex-html .mord').count()).toBeGreaterThan(0);
  // …and the MathML annotation echoes the source TeX (proves the right formula).
  const annotation = await page
    .locator('#formula .katex-mathml annotation')
    .first()
    .textContent();
  expect((annotation ?? '').replace(/\s+/g, '')).toBe(
    manifest.katex_formula_tex.replace(/\s+/g, ''),
  );

  // Chart.js: the live instance holds the seeded dataset, and the canvas painted.
  const chart = await page.evaluate(() => {
    const canvas = document.getElementById('chart') as HTMLCanvasElement;
    const Chart = (window as unknown as { Chart: { getChart(c: HTMLCanvasElement): unknown } })
      .Chart;
    const instance = Chart.getChart(canvas) as {
      data: { datasets: { data: number[] }[] };
      getDatasetMeta(i: number): { data: unknown[] };
    };
    const ctx = canvas.getContext('2d')!;
    const pixels = ctx.getImageData(0, 0, canvas.width, canvas.height).data;
    let painted = false;
    for (let i = 3; i < pixels.length; i += 4) {
      if (pixels[i] !== 0) {
        painted = true;
        break;
      }
    }
    return {
      data: instance.data.datasets[0].data,
      points: instance.getDatasetMeta(0).data.length,
      painted,
    };
  });
  expect(chart.data).toEqual(manifest.chart_data);
  expect(chart.points).toBe(manifest.chart_data.length);
  expect(chart.painted).toBe(true);
}

let server: CspServer;
test.beforeAll(async () => {
  server = await startCspServer(FIXTURES);
});
test.afterAll(async () => {
  await server?.close();
});

// ---------------------------------------------------------------------------
// A — hosted render under the exact artifact CSP
// ---------------------------------------------------------------------------
test('A: hosted artifact renders KaTeX + Chart.js under the artifact CSP', async ({ page }) => {
  await collectCspViolations(page);
  const response = await page.goto(server.url(manifest.artifact), { waitUntil: 'networkidle' });

  // The envelope is provably applied (the A7 bar: it must still render *and*
  // serve the CSP header), not merely "it rendered".
  expect(response?.headers()['content-security-policy']).toContain("default-src 'none'");
  expect(response?.headers()['content-security-policy']).toContain('sandbox allow-scripts');

  await assertCuratedRender(page);
  expect(await readViolations(page)).toEqual([]);
});

// ---------------------------------------------------------------------------
// B — offline export render (no network, no Access)
// ---------------------------------------------------------------------------
test('B: inline-exported standalone renders offline with no network', async ({ page, context }) => {
  await collectCspViolations(page);

  // Hard-prove "offline": abort every network request. A self-contained file
  // makes zero, so any attempt (a missed inline) fails the test loudly.
  const attempted: string[] = [];
  await context.route('**/*', (route) => {
    const url = route.request().url();
    if (!url.startsWith('file:')) {
      attempted.push(url);
      return route.abort();
    }
    return route.continue();
  });

  const fileUrl = pathToFileURL(join(FIXTURES, manifest.standalone)).href;
  await page.goto(fileUrl, { waitUntil: 'load' });

  await assertCuratedRender(page);
  expect(attempted, `standalone attempted network: ${attempted.join(', ')}`).toEqual([]);
  expect(await readViolations(page)).toEqual([]);
});

// ---------------------------------------------------------------------------
// C — paged.js eval-gate (record-only)
// ---------------------------------------------------------------------------
test('C: paged.js runs under the eval-free artifact CSP without an eval violation', async ({
  page,
}) => {
  await collectCspViolations(page);
  await page.goto(server.url(manifest.pagedjs), { waitUntil: 'networkidle' });

  // paged.js chunks the document into `.pagedjs_pages` when it executes; wait for
  // that as the "it ran" signal (it runs on load).
  await expect(page.locator('.pagedjs_pages')).toBeAttached({ timeout: 15_000 });

  // The eval-gate question: under a CSP that omits 'unsafe-eval', did paged.js
  // trip a script/eval CSP violation? (`eval` blocked → blockedURI 'eval'.)
  const evalViolations = (await readViolations(page)).filter(
    (v) => v.blockedURI === 'eval' || /eval/i.test(v.directive),
  );
  expect(
    evalViolations,
    `paged.js eval-gate verdict — violations: ${JSON.stringify(evalViolations)}`,
  ).toEqual([]);
});
