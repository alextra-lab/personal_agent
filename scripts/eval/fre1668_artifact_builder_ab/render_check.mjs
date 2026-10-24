// FRE-1668: headless render check for one saved artifact page.
// Usage: node render_check.mjs <html-file> <artifacts-origin|none> [substitution-map.json] [screenshot.png]
// Prints one JSON object on stdout. Requests to any host other than the artifacts origin are
// aborted and counted as blocked (this mimics connect-src 'none' plus the host allowlist of
// ADR-0089; it is not the exact served CSP).
// The artifacts origin sits behind Cloudflare Access, so an unauthenticated GET of /lib/ gets a 302
// to a login page. A /lib/ request is therefore answered from the pinned public-CDN twin in the
// substitution map (config/artifact_lib_substitution_map.json), which is what the Worker serves.
// A /lib/ path that the map does not list stays a failed request: the toolkit does not host it.
// An entry with no public twin (fonts) is answered with an empty 200 and listed as lib_unavailable.
import { createRequire } from "node:module";
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const require = createRequire(path.resolve(here, "../../../seshat-pwa/package.json"));
const { chromium } = require("playwright");

const [file, originArg, mapPath, shotPath] = process.argv.slice(2);
const libMap = new Map(
  mapPath ? JSON.parse(readFileSync(mapPath, "utf8")).entries.map((e) => [e.lib_path, e]) : [],
);
const libCache = new Map();
const TYPES = { script: "application/javascript", style: "text/css", font: "font/woff2" };
const origin = originArg && originArg !== "none" ? new URL(originArg).origin : null;
const html = readFileSync(file, "utf8");
const result = { page_errors: [], console_errors: [], failed_requests: [], blocked_requests: [], lib_unavailable: [], blank: false };

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
  await page.route("**/*", async (route) => {
    const url = route.request().url();
    if (url.startsWith("data:") || url.startsWith("about:") || url.startsWith("blob:")) return route.continue();
    if (origin && url.startsWith(origin + "/lib/") && libMap.size) {
      const entry = libMap.get(new URL(url).pathname.slice(1));
      if (!entry) {
        result.failed_requests.push(`not in toolkit: ${new URL(url).pathname}`);
        return route.abort();
      }
      if (!entry.public_cdn_url) {
        // Hosted by the Worker, but the bytes have no public twin here (fonts): answer empty.
        result.lib_unavailable.push(entry.lib_path);
        return route.fulfill({
          status: 200,
          body: "",
          headers: { "content-type": TYPES[entry.kind] ?? "application/octet-stream", "access-control-allow-origin": "*" },
        });
      }
      try {
        if (!libCache.has(entry.lib_path)) {
          const r = await fetch(entry.public_cdn_url);
          libCache.set(entry.lib_path, { status: r.status, body: Buffer.from(await r.arrayBuffer()), type: r.headers.get("content-type") });
        }
        const hit = libCache.get(entry.lib_path);
        return route.fulfill({
          status: hit.status,
          body: hit.body,
          headers: { "content-type": TYPES[entry.kind] ?? hit.type ?? "application/octet-stream", "access-control-allow-origin": "*" },
        });
      } catch (e) {
        result.failed_requests.push(`lib fetch failed: ${new URL(url).pathname}`);
        return route.abort();
      }
    }
    if (origin && (url === origin || url.startsWith(origin + "/"))) return route.continue();
    result.blocked_requests.push(url.slice(0, 200));
    return route.abort();
  });
  await page.setContent(html, { waitUntil: "networkidle", timeout: 30000 });
  await page.waitForTimeout(1500);
  if (shotPath) await page.screenshot({ path: shotPath, fullPage: true });
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
