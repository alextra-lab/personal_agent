// The publish check of a dev-agent page (ADR-0159 D7.3-D7.5; FRE-1686). Seshat owns this file.
//
//   node /opt/check/publish-check.mjs page    the load and generic interaction steps
//   node /opt/check/publish-check.mjs tests   the repo tests step
//
// The gateway runs the two modes in two separate check containers. The repo tests are code that
// the model wrote, so they never run in the container that judges the load and interaction steps.
//
// Inputs (read-only): /input/dist/index.html, /input/tests/, the toolkit at /srv/artifact-lib.
// Only dist/index.html is served, because production publishes only that file.
// The last stdout line is `SESHAT_CHECK_RESULT <json>`. The run has no network.
import { spawn } from 'node:child_process';
import { createHash } from 'node:crypto';
import {
  copyFileSync, cpSync, existsSync, mkdirSync, mkdtempSync, readFileSync, symlinkSync, writeFileSync,
} from 'node:fs';
import { join } from 'node:path';

import { chromium } from 'playwright';

import { startCspServer } from '/opt/csp/csp-server.ts';

const MANIFEST = '/opt/csp/csp-manifest.json';
const TOOLKIT = '/srv/artifact-lib';
const CHECK_DIR = '/opt/check';
const PREFIX = 'SESHAT_CHECK_RESULT ';
// Chromium does not know the `webrtc` directive of the production CSP, and logs it on every page
// (FRE-1682 README). This one message is not a page error. It is matched by its exact text and by
// its position (line 0, column 0, which no page script has), so page code cannot hide an error
// behind it.
const BENIGN = "Unrecognized Content-Security-Policy directive 'webrtc'.";

function benign(message) {
  const at = message.location();
  return message.text() === BENIGN && at.lineNumber === 0 && at.columnNumber === 0;
}
const MAX_MESSAGES = 50;
const MAX_MESSAGE_CHARS = 500;
const MAX_CONTROLS = 200;
const ACTION_TIMEOUT_MS = 2000;

function clip(messages) {
  return messages.slice(0, MAX_MESSAGES).map((m) => String(m).slice(0, MAX_MESSAGE_CHARS));
}

function step(name, passed, messages) {
  return { name, passed, messages: clip(messages) };
}

function finish(body) {
  console.log(PREFIX + JSON.stringify(body));
}

async function serve() {
  const root = mkdtempSync('/tmp/page-');
  copyFileSync('/input/dist/index.html', join(root, 'index.html'));
  if (existsSync(TOOLKIT)) symlinkSync(TOOLKIT, join(root, 'lib'));
  const server = await startCspServer(root, MANIFEST);
  return { server, url: server.url('/index.html') };
}

// One record of everything the page reports. Page code cannot clear it: the CSP listener holds the
// binding in a closure, and console and page errors come from the browser, not from page code.
async function watch(context, page) {
  const record = { console: [], page: [], csp: [] };
  await context.exposeBinding('__seshatCspViolation', (_source, text) => record.csp.push(text));
  await context.addInitScript(() => {
    const report = window.__seshatCspViolation;
    document.addEventListener(
      'securitypolicyviolation',
      (e) => report(`${e.effectiveDirective} blocked ${e.blockedURI || '(inline)'}`),
      true,
    );
  });
  page.on('console', (m) => {
    if (m.type() === 'error' && !benign(m)) record.console.push(m.text());
  });
  page.on('pageerror', (e) => record.page.push(String(e)));
  page.on('dialog', (d) => d.dismiss().catch(() => {}));
  return record;
}

function counts(record) {
  return { console: record.console.length, page: record.page.length, csp: record.csp.length };
}

function since(record, mark) {
  return [
    ...record.console.slice(mark.console).map((m) => `console error: ${m}`),
    ...record.page.slice(mark.page).map((m) => `page error: ${m}`),
    ...record.csp.slice(mark.csp).map((m) => `CSP violation: ${m}`),
  ];
}

// The generic interaction (D7.4). Deterministic: the same page gets the same actions, in document
// order, with fixed values. No model call.
async function operate(page, url) {
  const failures = [];
  const controls = page.locator('button, input, select, textarea, [role=button]');
  const total = Math.min(await controls.count(), MAX_CONTROLS);
  let operated = 0;
  for (let i = 0; i < total; i += 1) {
    const el = controls.nth(i);
    let kind = '';
    try {
      if (!(await el.isVisible()) || !(await el.isEnabled())) continue;
      const [tag, type, role] = await el.evaluate((e) => [
        e.tagName.toLowerCase(), (e.getAttribute('type') || 'text').toLowerCase(), e.getAttribute('role'),
      ]);
      kind = tag === 'input' ? `input[type=${type}]` : tag;
      const opts = { timeout: ACTION_TIMEOUT_MS };
      if (tag === 'button' || role === 'button' || (tag === 'input' &&
          ['button', 'submit', 'reset', 'image', 'checkbox', 'radio'].includes(type))) {
        await el.click(opts);
      } else if (tag === 'select') {
        const index = await el.evaluate((s) => {
          const other = [...s.options].findIndex((o, n) => n !== s.selectedIndex && !o.disabled);
          return other;
        });
        if (index < 0) continue;
        await el.selectOption({ index }, opts);
      } else if (tag === 'input' && type === 'range') {
        await el.evaluate((r) => {
          const max = r.max === '' ? '100' : r.max;
          const min = r.min === '' ? '0' : r.min;
          r.value = r.value === max ? min : max;
          r.dispatchEvent(new Event('input', { bubbles: true }));
          r.dispatchEvent(new Event('change', { bubbles: true }));
        });
      } else if (tag === 'input' && type === 'file') {
        continue;
      } else if (tag === 'input' && type === 'number') {
        await el.fill('7', opts);
      } else if (tag === 'input' && type === 'date') {
        await el.fill('2026-01-15', opts);
      } else if (tag === 'input' && type === 'color') {
        await el.fill('#336699', opts);
      } else {
        await el.fill('Seshat', opts);
      }
      operated += 1;
    } catch (e) {
      // A visible, enabled control that cannot be operated (covered by an overlay, never
      // actionable) is a fail: the step must operate every control (D7.4).
      failures.push(`not operated: control ${i} ${kind}: ${String(e).split('\n')[0]}`);
    }
    await page.waitForTimeout(100);
    if (page.url() !== url) {
      return { operated, failures, navigated: page.url() };
    }
  }
  await page.waitForTimeout(500);
  return { operated, failures, navigated: page.url() !== url ? page.url() : null };
}

async function pageMode() {
  const manifestSha = createHash('sha256').update(readFileSync(MANIFEST)).digest('hex');
  const { server, url } = await serve();
  const browser = await chromium.launch({ args: ['--disable-dev-shm-usage'] });
  const steps = [];
  try {
    const context = await browser.newContext();
    const page = await context.newPage();
    const record = await watch(context, page);
    try {
      await page.goto(url, { waitUntil: 'load', timeout: 30000 });
      await page.waitForTimeout(1000);
    } catch (e) {
      record.page.push(`the page did not load: ${String(e).split('\n')[0]}`);
    }
    const loaded = since(record, { console: 0, page: 0, csp: 0 });
    steps.push(step('load', loaded.length === 0, loaded));

    const mark = counts(record);
    const { operated, failures, navigated } = await operate(page, url);
    const found = [...since(record, mark), ...failures];
    if (navigated) found.push(`a control navigated away from the page, to ${navigated}`);
    steps.push(step('interaction', found.length === 0, [...found, `operated ${operated} controls`]));
  } finally {
    await browser.close();
    await server.close();
  }
  finish({ mode: 'page', csp_manifest_sha256: manifestSha, steps });
}

function errorsOf(node, out) {
  if (Array.isArray(node)) {
    for (const item of node) errorsOf(item, out);
  } else if (node && typeof node === 'object') {
    if (typeof node.message === 'string' && ('stack' in node || 'location' in node)) {
      out.push(node.message.split('\n')[0]);
    }
    for (const value of Object.values(node)) errorsOf(value, out);
  }
  return out;
}

async function testsMode() {
  const work = '/tmp/work';
  mkdirSync(work, { recursive: true });
  cpSync('/input/tests', join(work, 'tests'), { recursive: true });
  // The page template's package.json is an ES module package. The repo's own config is not used.
  writeFileSync(join(work, 'package.json'), '{"type":"module"}\n');
  symlinkSync(`${CHECK_DIR}/node_modules`, join(work, 'node_modules'));
  const { server, url } = await serve();
  let code;
  let tail = '';
  try {
    code = await new Promise((resolve) => {
      const child = spawn(
        'node',
        [`${CHECK_DIR}/node_modules/@playwright/test/cli.js`, 'test', '--config',
          `${CHECK_DIR}/repo-tests.config.mjs`],
        { cwd: work, env: { ...process.env, PAGE_URL: url }, stdio: ['ignore', 'pipe', 'pipe'] },
      );
      const keep = (chunk) => { tail = (tail + chunk.toString()).slice(-4000); };
      child.stdout.on('data', keep);
      child.stderr.on('data', keep);
      child.on('close', (c) => resolve(c ?? 1));
    });
  } finally {
    await server.close();
  }
  let stats = { expected: 0, unexpected: 0, flaky: 0, skipped: 0 };
  const messages = [];
  try {
    const report = JSON.parse(readFileSync(join(work, 'report.json'), 'utf-8'));
    stats = { ...stats, ...report.stats };
    messages.push(...errorsOf(report.suites ?? [], []), ...errorsOf(report.errors ?? [], []));
  } catch {
    messages.push('no test report');
  }
  const passed = code === 0 && stats.expected >= 1 && stats.unexpected === 0 && stats.flaky === 0;
  if (stats.expected + stats.unexpected + stats.flaky === 0) messages.unshift('the repo has no test');
  messages.unshift(
    `exit ${code}: ${stats.expected} passed, ${stats.unexpected} failed, ` +
    `${stats.flaky} flaky, ${stats.skipped} skipped`,
  );
  if (!passed) messages.push(tail.trim().split('\n').slice(-5).join(' | '));
  finish({ mode: 'tests', steps: [step('repo_tests', passed, messages)] });
}

const mode = process.argv[2];
if (mode === 'page') await pageMode();
else if (mode === 'tests') await testsMode();
else {
  console.error('usage: node publish-check.mjs page|tests');
  process.exit(2);
}
