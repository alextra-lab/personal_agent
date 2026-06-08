# FRE-527 — Artifact toolkit #2: in-repo slice (`/lib/` verify-envelope assertion)

**Linear:** FRE-527 (Approved · Tier-2:Sonnet · project *Artifact Execution Security*)
**ADR:** ADR-0089 Addendum A — step #2 (A3 hosting + A7 verify) · realizes D2a
**Builds on:** FRE-509 (Worker `/lib/` slot + MIME/nosniff/route controls), FRE-512 (`make verify-envelope`)
**Codex-reviewed:** 2026-06-08 (4 refinements adopted — see inline)

## Scope split (cross-repo)

| # | Deliverable | Repo | Owner |
|---|---|---|---|
| 1 | Host the curated `/lib/` assets (KaTeX/Chart.js/highlight.js/OFL fonts/paged.js + bespoke three.js IIFE) | `personal_agent_secrets` Worker | **laptop CC** (contract posted to FRE-527 as a Linear comment) |
| 2 | Export **substitution map** (SRI from pinned bytes, CORS-verified) | `personal_agent_secrets` (co-located w/ toolkit) | **laptop CC** |
| 3 | **Extend `make verify-envelope`** to assert `/lib/` assets serve executable-MIME + `nosniff` + reachable under the artifact CSP | **this repo** | **this build session** |

This plan covers **only #3**. The committed manifest (`config/artifact_lib_manifest.json`) is the single lockstep source both sides obey.

## Design

Artifact verification asserts an artifact must **never** serve an executable-script MIME. The `/lib/` rule is the **exact inverse**: toolkit JS **must** serve an executable JS MIME (else `nosniff` stops the browser executing it). Same canonical MIME set, opposite polarity at each use site → one source of truth.

"Reachable **under the artifact CSP**" is asserted concretely (codex #4): each asset's origin must be admitted by the matching `EXPECTED_CSP_DIRECTIVES` directive — `script→script-src`, `style→style-src`, `font→font-src` — **and** the asset must serve 200 + correct MIME + `nosniff`. This cross-checks the two lockstep surfaces (the served CSP and the hosted `/lib/` origin). Verifier consumes **status + headers only** (D1/D5 scope boundary — never bytes).

### Manifest (`config/artifact_lib_manifest.json`) — the lockstep contract (codex #3)

Machine-readable, checked-in, shared with the Worker repo. Adding an asset = one JSON entry, no code change. `spec.py` owns only the *schema + loader + invariants*. **Versions/theme/font picks below are PROPOSED — the laptop CC confirms them at hosting time and edits this one file.**

```json
{
  "origin": "https://artifacts.frenchforet.com",
  "assets": [
    {"name": "katex",            "path": "katex@0.16.22/katex.min.js",                 "kind": "script"},
    {"name": "katex",            "path": "katex@0.16.22/katex.min.css",                "kind": "style"},
    {"name": "chartjs",          "path": "chartjs@4.4.7/chart.umd.js",                 "kind": "script"},
    {"name": "highlightjs",      "path": "highlightjs@11.11.1/highlight.min.js",       "kind": "script"},
    {"name": "highlightjs",      "path": "highlightjs@11.11.1/github-dark.min.css",    "kind": "style"},
    {"name": "three",            "path": "three@0.171.0/three.iife.min.js",            "kind": "script"},
    {"name": "source-serif-4",   "path": "fonts/source-serif-4@4.005/source-serif-4.woff2",       "kind": "font"},
    {"name": "playfair-display", "path": "fonts/playfair-display@2.103/playfair-display.woff2",    "kind": "font"},
    {"name": "jetbrains-mono",   "path": "fonts/jetbrains-mono@2.304/jetbrains-mono.woff2",        "kind": "font"},
    {"name": "pagedjs",          "path": "pagedjs@0.4.3/paged.polyfill.min.js",        "kind": "script", "eval_gated": true}
  ]
}
```
Entry assets only (not every KaTeX font file — full render is FRE-531 E2E). `eval_gated` assets are excluded from the default assert set until the laptop CC confirms eval-free under the CSP.

## Steps (TDD — failing test first each step)

### 1 — `spec.py`: types, MIME invariants, manifest loader
File: `src/personal_agent/observability/artifact_envelope/spec.py`
- **Rename** `FORBIDDEN_SCRIPT_MIMES` → `EXECUTABLE_SCRIPT_MIMES` (canonical); keep `FORBIDDEN_SCRIPT_MIMES = EXECUTABLE_SCRIPT_MIMES` alias (used on the artifact-negation side + existing tests) (codex #1)
- `EXPECTED_STYLE_MIME = "text/css"`; `EXPECTED_FONT_MIMES = {".woff2":"font/woff2", ".woff":"font/woff", ".ttf":"font/ttf", ".otf":"font/otf"}`
- `LibAssetKind = Literal["script", "style", "font"]`
- `LIB_KIND_CSP_DIRECTIVE: Mapping[LibAssetKind, str] = {"script":"script-src","style":"style-src","font":"font-src"}`
- `@dataclass(frozen=True) class LibAsset:` `name:str`, `path:str`, `kind:LibAssetKind`, `eval_gated:bool=False`
- `DEFAULT_LIB_MANIFEST_PATH` (repo-root `config/artifact_lib_manifest.json`)
- `load_lib_manifest(path) -> tuple[str, tuple[LibAsset, ...]]` — returns `(origin, assets)`; lazy (no import-time I/O); raises `ValueError` on bad shape/kind/ext

### 2 — `verifier.py`: `verify_lib_asset`
File: `src/personal_agent/observability/artifact_envelope/verifier.py`
- `@dataclass(frozen=True) class LibAssetReport:` `asset_ok`, `name`, `path`, `kind`, `http_status`, `served_mime`, `mime_ok`, `nosniff_ok`, `csp_host_ok`, `failures`
- `verify_lib_asset(status_code, headers, *, asset, origin) -> LibAssetReport`:
  - `http_error` unless 200–299
  - MIME by kind: `script` → media-type ∈ `EXECUTABLE_SCRIPT_MIMES` (else `non_executable_script_mime`); `style` → `text/css` (else `wrong_mime`); `font` → `EXPECTED_FONT_MIMES[ext]` (else `wrong_mime`)
  - `missing_nosniff` unless `X-Content-Type-Options: nosniff`
  - `csp_host_not_allowed` unless `origin` ∈ host-sources of `EXPECTED_CSP_DIRECTIVES[LIB_KIND_CSP_DIRECTIVE[kind]]` (codex #4)
  - reuse `_as_pairs`/`_values`/`_parse_content_type`
- artifact `_check_mime` now references `EXECUTABLE_SCRIPT_MIMES` (negation) — behavior identical

### 3 — `__init__.py`: export `LibAsset`, `LibAssetKind`, `LibAssetReport`, `verify_lib_asset`, `load_lib_manifest`, `EXECUTABLE_SCRIPT_MIMES` (keep `FORBIDDEN_SCRIPT_MIMES`)

### 4 — `scripts/verify_artifact_envelope.py`: `--lib` mode (codex #2 — extends the same tool)
- `--lib` flag + `--manifest PATH` (default `DEFAULT_LIB_MANIFEST_PATH`). In `--lib` mode: load manifest, iterate assets (skip `eval_gated`), fetch `origin/lib/<path>` (headers-only stream; same Access-token + `classify_access_denied` handling), `verify_lib_asset` each, print per-asset report, exit non-zero on any failure (Access-denied = unverifiable → 1). Positional `url` becomes optional when `--lib` is set.

### 5 — Makefile: `verify-lib` + tracked fold-in note
```make
verify-lib:
	@uv run python scripts/verify_artifact_envelope.py --lib $(if $(MANIFEST),--manifest $(MANIFEST),)
```
Comment on `verify-envelope`: `# Toolkit /lib/ assertion lives in `make verify-lib` (FRE-527); fold into this gate once the Worker hosts /lib/ (master, post-hosting).` Keeps the live FRE-512 gate green until hosting lands.

### 6 — Tests
File: `tests/observability/artifact_envelope/test_verifier.py` (extend)
- happy paths: script `text/javascript` / style `text/css` / font `font/woff2` → `asset_ok`
- failures: script `text/plain`→`non_executable_script_mime`; wrong style/font MIME; missing nosniff; 404→`http_error`; missing Content-Type; `origin` not in CSP directive → `csp_host_not_allowed`
- **polarity test**: a media-type in `EXECUTABLE_SCRIPT_MIMES` passes `verify_lib_asset(kind=script)` AND fails `verify_envelope` for an artifact
- back-compat: `FORBIDDEN_SCRIPT_MIMES is EXECUTABLE_SCRIPT_MIMES`
- **manifest shape**: `load_lib_manifest(DEFAULT_LIB_MANIFEST_PATH)` parses; every kind valid; font paths have a known ext; origin admitted by `script-src`/`style-src`/`font-src` in `EXPECTED_CSP_DIRECTIVES`

## Verify
```bash
uv run python -m pytest tests/observability/artifact_envelope/test_verifier.py -q   # all pass
make mypy
make ruff-check && make ruff-format
uv run python -m pytest -m "not integration" -q                                     # full unit suite green
# Live (laptop CC, post-hosting): make verify-lib   → exit 0 once /lib/ is served
```

## Out of scope (handed to laptop CC via Linear comment)
Hosting the assets (#1); substitution map from pinned bytes (#2); three.js IIFE build; paged.js eval-free confirmation; final version/theme/font picks (edited into `config/artifact_lib_manifest.json`).
