// The Playwright config of the repo tests step of the publish check (ADR-0159 D7.4; FRE-1686).
// Seshat owns it. The repo's own playwright.config.mjs is not exported to the check container.
// publish-check.mjs serves dist/index.html with the production CSP and sets PAGE_URL.
import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: '/tmp/work/tests',
  // The test contract of the page template's AGENTS.md.
  testMatch: '**/*.@(spec|test).mjs',
  outputDir: '/tmp/work/results',
  workers: 1,
  retries: 0,
  timeout: 30000,
  globalTimeout: 150000,
  forbidOnly: true,
  reporter: [['json', { outputFile: '/tmp/work/report.json' }]],
  use: {
    headless: true,
    // /dev/shm is small in the check container.
    launchOptions: { args: ['--disable-dev-shm-usage'] },
  },
});
