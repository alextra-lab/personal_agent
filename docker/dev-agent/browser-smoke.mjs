// Loads one page in headless Chromium, clicks #b, and prints a JSON line (FRE-1682).
// A CSP violation is logged by Chromium as a console error, so it shows up in `errors`.
import { chromium } from 'playwright';

const url = process.argv[2];
const browser = await chromium.launch({ args: ['--disable-dev-shm-usage'] });
try {
  const page = await browser.newPage();
  const errors = [];
  page.on('console', (message) => {
    if (message.type() === 'error') errors.push(message.text());
  });
  page.on('pageerror', (error) => errors.push(String(error)));
  await page.goto(url);
  await page.click('#b');
  await page.waitForTimeout(500);
  const text = await page.textContent('#out');
  console.log(JSON.stringify({ page: url.split('/').pop(), text, errors }));
} finally {
  await browser.close();
}
