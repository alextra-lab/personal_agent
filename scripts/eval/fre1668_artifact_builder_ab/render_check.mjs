// FRE-1668: headless render check and widget check for one saved artifact page.
// Usage: node render_check.mjs <html-file> <artifacts-origin|none> [substitution-map.json] [screenshot.png]
// Prints one JSON object on stdout.
//
// Render check. Requests to any host other than the artifacts origin are aborted and counted as
// blocked (this mimics connect-src 'none' plus the host allowlist of ADR-0089; it is not the exact
// served CSP). The artifacts origin sits behind Cloudflare Access, so an unauthenticated GET of
// /lib/ gets a 302 to a login page. A /lib/ request is therefore answered from the pinned
// public-CDN twin in the substitution map (config/artifact_lib_substitution_map.json), which is
// what the Worker serves. A /lib/ path that the map does not list stays a failed request: the
// toolkit does not host it. An entry with no public twin (fonts) is answered with an empty 200 and
// listed as lib_unavailable.
//
// Widget check. After the page loads, the script finds every visible control (buttons, range
// sliders, selects, checkboxes, radios, text and number inputs, textareas, role=button, summary),
// operates each one in turn, and records per control whether the page reacted and whether a page
// error or console error appeared. The reaction test compares a signature of the DOM and of every
// canvas before and after the operation. A page that changes by itself (an animation, a clock)
// cannot show a clean reaction on that surface. The check reads an idle drift just before each
// operation. A control with no clean change on a drifting page is "unverifiable", not "no".
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
const result = {
  page_errors: [], console_errors: [], failed_requests: [], blocked_requests: [], lib_unavailable: [],
  blank: false,
  widgets: { found: 0, worked: 0, no_reaction: 0, unverifiable: 0, not_operated: 0, with_errors: 0, errors: 0, capped: false, controls: [] },
};

const CONTROL_SEL = 'button, input, select, textarea, [role="button"], summary';
const SKIP_TYPES = ["hidden", "file", "color", "image", "reset"];
const MAX_CONTROLS = 60;
const WIDGET_BUDGET_MS = 120000;

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
  page.on("dialog", (d) => d.dismiss().catch(() => {}));
  await page.addInitScript(() => {
    window.addEventListener("submit", (e) => e.preventDefault(), true);
    window.open = () => null;
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

  // ---- widget check ----
  const loadErrors = { page: result.page_errors.length, console: result.console_errors.length };
  const signature = () =>
    page.evaluate(() => {
      const hash = (s) => {
        let h = 5381;
        for (let i = 0; i < s.length; i++) h = ((h << 5) + h + s.charCodeAt(i)) | 0;
        return h + ":" + s.length;
      };
      let canvases = "";
      document.querySelectorAll("canvas").forEach((c) => {
        try { canvases += hash(c.toDataURL()) + ","; } catch (e) { canvases += "x,"; }
      });
      return { dom: hash(document.body.innerHTML), canvas: canvases };
    });
  const listControls = () =>
    page.evaluate(
      ({ sel, skip }) => {
        const out = [];
        document.querySelectorAll(sel).forEach((el, index) => {
          const r = el.getBoundingClientRect();
          const st = getComputedStyle(el);
          const tag = el.tagName.toLowerCase();
          const type = tag === "input" ? (el.type || "text") : tag === "button" ? "button" : tag;
          const visible = r.width > 0 && r.height > 0 && st.visibility !== "hidden" && st.display !== "none";
          if (!visible || el.disabled || skip.includes(type)) return;
          const label = (el.getAttribute("aria-label") || (el.labels && el.labels[0] ? el.labels[0].innerText : "") ||
            el.innerText || el.value || el.id || el.name || el.placeholder || "").toString().trim().replace(/\s+/g, " ").slice(0, 40);
          out.push({ index, type, label });
        });
        return out;
      },
      { sel: CONTROL_SEL, skip: SKIP_TYPES },
    );
  const operate = async (c) => {
    const loc = page.locator(CONTROL_SEL).nth(c.index);
    if (c.type === "range") {
      await loc.evaluate((el) => {
        const lo = Number(el.min || 0), hi = Number(el.max || 100), cur = Number(el.value);
        el.value = String(cur - lo > (hi - lo) / 2 ? lo : hi);
        el.dispatchEvent(new Event("input", { bubbles: true }));
        el.dispatchEvent(new Event("change", { bubbles: true }));
      });
    } else if (c.type === "select") {
      const next = await loc.evaluate((el) => {
        const opts = [...el.options].filter((o) => !o.disabled);
        const i = opts.findIndex((o) => o.value === el.value);
        return (opts[(i + 1) % opts.length] || {}).value;
      });
      await loc.selectOption(next, { timeout: 2000 });
    } else if (c.type === "number") {
      await loc.fill("7", { timeout: 2000 });
    } else if (["text", "search", "email", "url", "tel", "password", "textarea"].includes(c.type)) {
      await loc.fill("test", { timeout: 2000 });
      await loc.dispatchEvent("change");
    } else if (c.type === "date") {
      await loc.fill("2024-01-15", { timeout: 2000 });
    } else {
      await loc.click({ timeout: 2000 });
    }
  };

  const probe = async (c) => {
    const rec = { kind: c.type, label: c.label, reacted: "no", errors: 0 };
    try {
      const a = await signature();
      await page.waitForTimeout(150);
      const b = await signature();
      const driftDom = a.dom !== b.dom, driftCanvas = a.canvas !== b.canvas;
      const e0 = result.page_errors.length + result.console_errors.length;
      await operate(c);
      await page.waitForTimeout(400);
      const d = await signature();
      rec.errors = result.page_errors.length + result.console_errors.length - e0;
      const cleanDom = d.dom !== b.dom && !driftDom;
      const cleanCanvas = d.canvas !== b.canvas && !driftCanvas;
      rec.reacted = cleanDom || cleanCanvas ? "yes" : driftDom || driftCanvas ? "unverifiable" : "no";
    } catch (e) {
      rec.reacted = "not_operated";
      rec.error = String(e.message ?? e).split("\n")[0].slice(0, 160);
    }
    return rec;
  };
  const settle = async () => {
    for (let i = 0; i < 8; i++) {
      const a = await signature();
      await page.waitForTimeout(400);
      const b = await signature();
      if (a.dom === b.dom && a.canvas === b.canvas) return;
    }
  };
  const RANK = { yes: 3, unverifiable: 2, no: 1, not_operated: 0 };
  const started = Date.now();
  // Phase 1: operate every control that is not reset-like, once, in page order.
  // Phase 2: a control may only act once the page holds state (a run, a selection). Let the page
  //   settle, then operate again each control that showed no clean reaction. Keep the best result.
  // Phase 3: operate the reset-like controls last, because they wipe the state the others act on.
  const RESET_LIKE = /reset|clear|restart|start over|new game|delete|remove/i;
  const done = new Set();
  const nextOf = async (wantReset) =>
    (await listControls()).find((c) => !done.has(c.index) && RESET_LIKE.test(c.label) === wantReset);
  const runPhase = async (wantReset) => {
    for (let n = 0; n < MAX_CONTROLS; n++) {
      if (Date.now() - started > WIDGET_BUDGET_MS) { result.widgets.capped = true; return; }
      const c = await nextOf(wantReset);
      if (!c) return;
      done.add(c.index);
      const rec = await probe(c);
      rec.index = c.index;
      result.widgets.controls.push(rec);
    }
    result.widgets.capped = true;
  };
  await runPhase(false);
  await settle();
  for (const rec of result.widgets.controls) {
    if (rec.reacted === "yes" || Date.now() - started > WIDGET_BUDGET_MS) continue;
    const match = (await listControls()).find((c) => c.index === rec.index && c.type === rec.kind);
    if (!match) continue;
    const again = await probe(match);
    rec.errors += again.errors;
    if (RANK[again.reacted] > RANK[rec.reacted]) { rec.reacted = again.reacted; delete rec.error; }
    await settle();
  }
  await runPhase(true);
  const w = result.widgets;
  w.found = w.controls.length;
  w.worked = w.controls.filter((c) => c.reacted === "yes" && c.errors === 0).length;
  w.no_reaction = w.controls.filter((c) => c.reacted === "no").length;
  w.unverifiable = w.controls.filter((c) => c.reacted === "unverifiable").length;
  w.not_operated = w.controls.filter((c) => c.reacted === "not_operated").length;
  w.with_errors = w.controls.filter((c) => c.errors > 0).length;
  w.errors = result.page_errors.length - loadErrors.page + (result.console_errors.length - loadErrors.console);
  // Errors raised while operating controls belong to the widget check, not to the load check.
  result.page_errors = result.page_errors.slice(0, loadErrors.page);
  result.console_errors = result.console_errors.slice(0, loadErrors.console);
} catch (e) {
  result.page_errors.push(`load: ${String(e.message ?? e).slice(0, 300)}`);
} finally {
  await browser.close();
}
console.log(JSON.stringify(result));
