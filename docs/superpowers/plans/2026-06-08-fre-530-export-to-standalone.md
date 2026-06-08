# FRE-530 — Export-to-standalone: inline (offline) + substitution-map → CDN+SRI (online)

**Linear:** FRE-530 (Approved, Tier-2:Sonnet, project *Artifact Execution Security*)
**ADR:** ADR-0089 Addendum **A5** (`docs/architecture_decisions/ADR-0089-artifact-execution-security-model.md` §317–325, §350)
**Depends on:** FRE-527 (`/lib/` hosted + `config/artifact_lib_substitution_map.json` — shipped/live)
**Blocks:** FRE-531 (E2E render + export verification)

---

## 1. Scope (what this ticket builds)

A **server-side export endpoint** that turns a hosted, `/lib/`-referencing artifact into a portable HTML file, in two modes:

- **Inline (default, offline-portable):** fetch each referenced pinned `/lib/` asset (+ its CSS subresources, e.g. KaTeX fonts) and inline it (scripts → inline `<script>`, CSS → inline `<style>`, fonts/url() → `data:` URIs) → one self-contained file that renders with **no network**.
- **Substitute (opt-in, lean/online):** rewrite each `…/lib/<name>@<version>` reference to the **same version on a public CDN**, adding `integrity="sha384-…"` (from our pinned bytes, via the map) + `crossorigin="anonymous"`. **Per-asset rule:** substitute only where `{public twin @ pinned version + SRI + cors_verified=true}`; otherwise **fall back to inline**. (three.js + fonts are inline-only; map already encodes this.)

Both modes return `Content-Disposition: attachment` and document the **honest limit**: an exported file leaves the envelope (runs unsandboxed wherever opened); substitute exports depend on a third-party CDN at view time (bounded by SRI).

### Locus decision (why Python gateway, not the Worker)
ADR §350 names a "Worker endpoint." The actual Cloudflare Worker source lives in the **private `personal_agent_secrets` repo** — not present, buildable, or `make test`-able in this build session. The substitution map note explicitly calls this repo's copy "the runtime mirror **consumed by FRE-530**," and the gateway already owns every primitive: R2 byte read (`R2ArtifactStore.get`), CF Access **service-token** fetch of the Access-gated artifact origin (`probe._service_token_headers`), and router infra (`artifacts_router.py`). → Build the export as a **CF-Access-gated gateway endpoint** + a dependency-injected, fully unit-testable transform module. (If the owner wants the trigger surfaced through the Worker later, the Worker just proxies to this endpoint — separate, secrets-repo work.)

---

## 2. Key design decisions (revised after codex review — confirm at approval)

1. **Inline byte source = SRI makes source-equivalent.** For a `/lib/` asset **with** a `public_cdn_url`, inline mode fetches the **parent bytes from the CDN base** and **SRI-verifies them against the map's pinned `sha384`** — SRI guarantees they are byte-identical to our pinned `/lib/` copy, and the CDN base lets the asset's own relative subresources (KaTeX fonts) resolve. For inline-only assets (three.js, fonts — no CDN twin), fetch from **our `/lib/` origin** and SRI-verify. Either way the inlined primary is SRI-anchored; a mismatch → **502, export fails** (never ship un-pinned bytes).
2. **CSS subresources (the KaTeX-fonts wrinkle), hardened.** `katex.min.css` references `url(fonts/KaTeX_*.woff2)` relatively; those files are **not** in our manifest/map. Inline mode inlines them as `data:` URIs, fetched at **export** time (never view time), under a **strict guard** so the un-pinned subresource fetch can't be abused:
   - resolve **relative** `url(...)` only; pass `data:` through untouched; **reject** absolute/`//`/`javascript:`/other-scheme refs (leave untouched + warn);
   - the resolved URL **must stay under the parent asset's CDN base dir** (prefix check — no path-escape, no host change);
   - **extension allowlist** `{woff2, woff, ttf, otf}` + **per-file size cap** (2 MB) + **total inlined cap** (8 MB) + per-fetch timeout;
   - trust chain: the parent CSS is SRI-pinned, and it dictates which same-version-dir fonts to pull. *(Per-font hash pinning would require extending the FRE-527 map — noted as future hardening, out of scope here.)*
3. **Substitute mode is browser-side-SRI (explicit decision).** Substitute mode does **not** fetch CDN bytes at export time — it rewrites the URL + emits `integrity="<our pinned sha384>"` + `crossorigin="anonymous"` from the map alone. The **browser** enforces SRI at view time (and blocks on CORS failure). This keeps the export lean/offline-of-our-infra and is what makes the file tiny + human-editable; the cost is that a CDN that later drifts from our pinned hash makes the asset fail to load (fail-closed in the browser, never a silent wrong-bytes load). Stated, not assumed.
4. **HTML rewrite = attribute-preserving, in place.** Rewrites target the specific `src=`/`href=` URL (and `url(...)`) that points at our origin's `/lib/`, matched with a tolerant regex (case-insensitive, `DOTALL`, single/double/unquoted, and the three ref forms: full `https://origin/lib/…`, protocol-relative `//origin/lib/…`, absolute-path `/lib/…`). Mirrors the proven `_SCRIPT_SRC_RE` in `artifact_tools.py`.
   - **Substitute:** edit the URL **in place** and append `integrity`/`crossorigin` **only if absent** (no duplicate attrs); **preserve** `defer`/`async`/`type`/`id`/`nonce`/etc.
   - **Inline:** `<script src>`→inline `<script>` **at the same position** (preserves source order); `type` preserved; `defer`/`async` semantics collapse to synchronous-in-place (documented limitation — our `/lib/` libs are order-tolerant classic scripts). `<link>`→`<style>`.
   - Anchor matches on a real attribute boundary so `data-src` / comments / JSON strings don't false-positive.
5. **Unmapped `/lib/` ref → fail-closed.** A reference to our origin's `/lib/` whose path is **not** in the map → `ArtifactExportError` → **502**, naming the path. A "portable" file that still points at the Access-gated origin silently breaks the offline/online promise, so surfacing map-drift is correct. (Refs to other origins are left untouched — not our concern.)
6. **Endpoint auth + scope.** `GET /api/v1/artifacts/{id}/export?mode=inline|substitute`, CF-Access-gated via `get_request_user`, owner-scoped (cross-user → 404, existence-hiding per ADR-0064 D3). Non-HTML artifact → 400. Bad mode → 422. Default `mode=inline`. Export never mutates the stored artifact.
7. **paged.js — not in the offline-inline acceptance bar.** It is `eval_gated` and may dynamically load resources at runtime; it is mapped mechanically (it has cdn+cors+sri) so substitute mode rewrites it, but **offline-inline portability for paged.js is deferred to FRE-531** and excluded from this ticket's acceptance.
8. **Shared service-token helper (no copy).** Extract `cf_access_service_token_headers() -> dict[str, str]` once and use it in the new fetcher; refactor `observability/artifact_envelope/probe.py` to import it (trivial, safe dedup the reviewer asked for).
9. **Service-token authorization caveat (cross-repo, post-deploy).** The CF Access service token must be authorized on the artifacts Access app policy (terraform, `personal_agent_secrets`) for the live endpoint to fetch `/lib/`. Unit tests inject the fetcher and are unaffected; live E2E is FRE-531. Documented as a Linear follow-up note, not a PR blocker.

---

## 3. Files

### 3.1 New — `src/personal_agent/storage/artifact_export.py`
Pure, dependency-injected transform (no FastAPI, no httpx import at module top — fetcher is a Protocol).

- `ExportMode = Literal["inline", "substitute"]`
- `@dataclass(frozen=True) class LibAsset` — `lib_path, name, kind, loading, public_cdn_url: str | None, sri: str, cors_verified: bool, fallback: str`.
- `@dataclass(frozen=True) class SubstitutionMap` — `origin: str`, `entries: tuple[LibAsset, ...]`; `by_lib_path: dict[str, LibAsset]`. `load_substitution_map(path: Path | None = None) -> SubstitutionMap` (reads `config/artifact_lib_substitution_map.json`; module-level `@lru_cache`-style cache).
- `class AssetFetcher(Protocol): async def fetch(self, url: str) -> bytes` — raises `ArtifactExportError` on non-200.
- `def compute_sri(data: bytes, algo: str = "sha384") -> str` → `"sha384-<b64>"`; `def verify_sri(data: bytes, expected: str) -> bool`.
- `class ArtifactExportError(RuntimeError)`.
- `async def export_artifact_html(*, html: str, mode: ExportMode, sub_map: SubstitutionMap, fetcher: AssetFetcher) -> str` — the core:
  - Regexes (case-insensitive): `<script ... src="<origin>/lib/...">...</script>`, `<link ... href="<origin>/lib/...">`, and `url( <origin>/lib/... )` (fonts). Match only the configured `sub_map.origin` + `/lib/` path; map URL → `LibAsset` by lib_path (strip origin).
  - Replace right-to-left (positions stay valid; mirrors `_render_mermaid_blocks`).
  - Unmapped `/lib/` path under our origin → `ArtifactExportError` (§2.5, fail-closed).
  - `_resolve_primary_source(asset)` → CDN base when `public_cdn_url` set, else the `/lib/` URL (§2.1 — SRI makes them byte-equivalent and the CDN base lets subresources resolve).
  - `_inline_script(asset, fetcher)` → fetch + **SRI-verify** → inline `<script {preserved type}>…</script>` **at the same position**; `_inline_style` → fetch + SRI-verify CSS → `_inline_css_subresources` → `<style>…</style>`; `_inline_font_url` → fetch + SRI-verify → `data:font/woff2;base64,…`.
  - `_inline_css_subresources(css, base_url, fetcher, budget)` → for each **relative** `url(...)`: pass `data:` through; **reject** other schemes / base-prefix escapes (leave + warn); enforce ext-allowlist `{woff2,woff,ttf,otf}` + per-file (2 MB) + total (8 MB) caps; fetch and replace with `data:<mime>;base64,…` (§2.2).
  - `_substitute(asset)` → if `public_cdn_url and cors_verified and kind in {script,style}`: edit the URL **in place**, append `integrity="{sri}" crossorigin="anonymous"` **only if absent**, **preserve all other attributes** (§2.4); else return `None` → caller inlines that asset.
  - Constants: `_SUBRESOURCE_EXTS`, `_MAX_SUBRESOURCE_BYTES = 2*1024*1024`, `_MAX_TOTAL_INLINE_BYTES = 8*1024*1024`, MIME-by-ext table.

### 3.2 Edit — `src/personal_agent/service/artifacts_router.py`
- New `GET /api/v1/artifacts/{artifact_id}/export` (tags `artifacts-public`), `mode: ExportMode = Query("inline")`.
- Resolve owned artifact row (reuse the owner-scoped select); require `content_type` startswith `text/html` else `HTTPException(400)`.
- `store = get_artifact_store()`; `raw = await store.get(row.r2_key, trace_id=...)`; `html = raw.decode("utf-8")`.
- Build `_HttpAssetFetcher` (httpx `AsyncClient`, per-request timeout): attach the **shared** `cf_access_service_token_headers()` (new `service/cf_service_token.py`, extracted from `probe._service_token_headers` and the probe refactored to import it — codex (a)) **only** for the `sub_map.origin` host; plain for CDN hosts; raises `ArtifactExportError` on non-200.
- `out = await export_artifact_html(html=html, mode=mode, sub_map=load_substitution_map(), fetcher=fetcher)`.
- Return `fastapi.Response(content=out, media_type="text/html; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="{slug or artifact_id}.html"', "X-Artifact-Export-Mode": mode, "X-Artifact-Export-Note": "leaves-sealed-envelope; runs-unsandboxed-when-opened"})`.
- `try/except ArtifactExportError` → `HTTPException(502, detail=...)`.
- Emit `log.info("artifact_export", trace_id=ctx.trace_id, user_id=..., artifact_id=..., mode=..., size_in=..., size_out=...)` (ADR-0074 identity threading).

### 3.3 New — `tests/personal_agent/storage/test_artifact_export.py`
Fake `AssetFetcher` (dict url→bytes, with a real SRI computed for fixtures); load the **real** `config/artifact_lib_substitution_map.json`.
- inline: script → inline `<script>` (no `src`), bytes present, **`type` preserved**; a `defer`/`async` fixture asserts in-place position.
- inline: style → inline `<style>`; a CSS `url(fonts/KaTeX_x.woff2)` subresource becomes a **real `data:font/woff2;base64,…`** (codex e1).
- inline: subresource with non-allowlisted ext / base-prefix escape → left untouched, no fetch.
- inline: font `url(<origin>/lib/fonts/...woff2)` → `data:` URI.
- inline: SRI mismatch on primary → `ArtifactExportError`.
- inline: KaTeX+Chart.js doc → **zero** remaining `artifacts.frenchforet.com` refs (offline acceptance).
- **unmapped** `/lib/foo@9/bar.js` → `ArtifactExportError` (§2.5).
- substitute: katex/chartjs → CDN URL + `integrity=` + `crossorigin=`; an **already-present `integrity` is not duplicated**.
- substitute: three.js (cdn null) + font (cors false) → inline fallback.
- `compute_sri`/`verify_sri` round-trip.

### 3.4 New — `tests/personal_agent/service/test_artifacts_router_export.py`
TestClient minimal app (mirror `test_artifacts_router.py`): override DB to return an owned `text/html` row; monkeypatch `get_artifact_store` → fake store returning canned HTML bytes; monkeypatch the module's fetcher class → fake fetcher.
- owned html + `mode=inline` → 200, `Content-Disposition: attachment`, body transformed.
- `mode=substitute` → 200, body has CDN+integrity.
- non-html artifact → 400.
- cross-user / missing → 404.
- `mode=bogus` → 422 (Literal validation).
- fetcher raises `ArtifactExportError` (origin/CDN unreachable) → **502** (codex e3,e4).

### 3.5 Docs
- `docs/skills/artifact-design.md` — extend the portability/export section: the two-mode endpoint, the per-asset substitute/inline rule, the KaTeX-subresource note, and the honest envelope-stripping limit + "never bake secrets into an artifact."
- `artifacts_router.py` module docstring — add the export endpoint to the surface description.

---

## 4. Build sequence (TDD, atomic)

0. Extract `cf_access_service_token_headers()` → `service/cf_service_token.py`; refactor `probe.py` to import it → `make test-file FILE=tests/observability/artifact_envelope` stays green.
1. Write `test_artifact_export.py` (failing — module absent) → `make test-file FILE=tests/personal_agent/storage/test_artifact_export.py` → **ImportError/red**.
2. Implement `artifact_export.py` until that file is **green**.
3. Write `test_artifacts_router_export.py` (failing — route 404) → red.
4. Add the `/export` endpoint + `_HttpAssetFetcher` until **green**.
5. Docs (3.5).
6. Quality gates: `make test` · `make mypy` · `make ruff-check` · `make ruff-format` · `pre-commit run --all-files`.
7. PR (template) — pre-merge checklist only. **STOP.**

## 5. Acceptance (ticket)
- [ ] Inline export of a KaTeX+Chart.js artifact renders **offline** (unit: all `/lib/` refs replaced by inline/data:, **zero** remaining `artifacts.frenchforet.com` references; KaTeX fonts present as real `data:` URIs).
- [ ] Substitute export references CDN + SRI (+ `crossorigin`); a CORS-lacking asset (font/three.js) falls back to inline (unit asserts both); browser-side-SRI enforcement is the stated posture (§2.3).
- [ ] Endpoint owner-scoped + non-HTML → 400 + bad-mode → 422 + unreachable-fetch → 502 + attachment disposition.
- [ ] Unmapped `/lib/` ref fails closed (502/`ArtifactExportError`); script attributes (`type`/`defer`) preserved through substitute.
- [ ] **paged.js offline-inline portability is NOT asserted here** (eval_gated, dynamic loader) — deferred to FRE-531 (§2.7).

## 6. Out of scope (named)
PWA download-button UI; the Worker proxy trigger; live E2E under real CSP (FRE-531); the D4a model-bridge; public/Access-excluded `/lib/`.
