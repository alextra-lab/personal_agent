// Playwright setup of a page repo (ADR-0159 D7.2, D8.5).
// The global setup serves dist/ with the production CSP, and sets PAGE_URL for the tests.
import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: 'tests',
  globalSetup: './playwright/serve.mjs',
  reporter: 'line',
  use: {
    headless: true,
    // /dev/shm is small in the job containers.
    launchOptions: { args: ['--disable-dev-shm-usage'] },
  },
});
