# Page repo: how to work here

You build or fix one interactive HTML page. The page is `dist/index.html`. Seshat publishes only that file.

## Rules

- Put the whole page in `dist/index.html`: HTML, CSS and JavaScript in one file.
- The page runs under a strict Content Security Policy. It cannot call `fetch()`, open a socket, or load a script, style or font from another site. Load a library only from the curated toolkit, as `/lib/<path>` (the files are in `/srv/artifact-lib`).
- Commit your work on the current branch. Do not create, rename or delete branches. Seshat takes only this branch.
- Do not commit symlinks or submodules. Seshat refuses a result that holds one.
- Keep `dist/index.html` at 5 MB or less.
- The network reaches only `registry.npmjs.org`, through the proxy that is already set.

## The test loop (required)

Repeat until the page works:

1. Open the page in a headless browser under the production CSP: `npm test` serves `dist/` with the CSP test server.
2. Operate every control: click every button, change every input, select, checkbox and range.
3. Read the console errors, the page errors and the CSP violations.
4. Fix the page. Go to step 1.

The page is done when it loads with zero console errors, zero page errors and zero CSP violations, and every control does what the task asks.

## The test contract (required)

`tests/` holds at least one Playwright test. Each test operates the page's controls and asserts their effect. A test that only loads the page is not enough. Seshat runs these tests before it publishes the page, with no network. A repo with no test fails.

Example:

```js
// tests/page.spec.mjs
import { test, expect } from '@playwright/test';

test('the add button adds a row', async ({ page }) => {
  const errors = [];
  page.on('pageerror', (e) => errors.push(e.message));
  await page.goto(process.env.PAGE_URL);
  await page.getByRole('button', { name: 'Add' }).click();
  await expect(page.locator('tbody tr')).toHaveCount(1);
  expect(errors).toEqual([]);
});
```

Set up once: `npm install`. Run: `npm test`.
