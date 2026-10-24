// FRE-1668: headless render check for one saved artifact page.
// Usage: node render_check.mjs <html-file> <artifacts-origin|none>
// Prints one JSON object on stdout. Requests to any host other than the artifacts origin are
// aborted and counted as blocked (this mimics connect-src 'none' plus the host allowlist of
// ADR-0089; it is not the exact served CSP).
import { createRequire } from "node:module";
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const require = createRequire(path.resolve(here, "../../../seshat-pwa/package.json"));
const { chromium } = require("playwright");

const [file, originArg] = process.argv.slice(2);
const origin = originArg && originArg !== "none" ? new URL(originArg).origin : null;
const html = readFileSync(file, "utf8");
const result = { page_errors: [], console_errors: [], failed_requests: [], blocked_requests: [], blank: false };

const browser = await chromium.launch();
try {
  const page = await browser.newPage();
  page.on("pageerror", (e) => result.page_errors.push(String(e.message ?? e).slice(0, 300)));
  page.on("console", (m) => {
    if (m.type() === "error") result.console_errors.push(m.text().slice(0, 300));
  });
  page.on("requestfailed", (r) => {
    const url = r.url();
    if (!url.startsWith("about:") && !url.startsWith("data:") && !result.blocked_requests.includes(url)) {
      result.failed_requests.push(url.slice(0, 200));
    }
  });
  page.on("response", (r) => {
    if (r.status() >= 400) result.failed_requests.push(`${r.status()} ${r.url().slice(0, 200)}`);
  });
  await page.route("**/*", (route) => {
    const url = route.request().url();
    if (url.startsWith("data:") || url.startsWith("about:") || url.startsWith("blob:")) return route.continue();
    if (origin && url.startsWith(origin)) return route.continue();
    result.blocked_requests.push(url.slice(0, 200));
    return route.abort();
  });
  await page.setContent(html, { waitUntil: "networkidle", timeout: 30000 });
  await page.waitForTimeout(1500);
  const seen = await page.evaluate(() => ({
    text: (document.body?.innerText ?? "").trim().length,
    visuals: document.querySelectorAll("canvas, svg, img").length,
  }));
  result.blank = seen.text < 40 && seen.visuals === 0;
  // A blocked request also fires requestfailed; drop those so each problem is counted once.
  result.failed_requests = result.failed_requests.filter(
    (u) => !result.blocked_requests.some((b) => u.endsWith(b) || u === b),
  );
} catch (e) {
  result.page_errors.push(`load: ${String(e.message ?? e).slice(0, 300)}`);
} finally {
  await browser.close();
}
console.log(JSON.stringify(result));
