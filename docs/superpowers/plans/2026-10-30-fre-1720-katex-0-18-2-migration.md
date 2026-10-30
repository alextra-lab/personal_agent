# FRE-1720 — katex 0.16.47 → 0.18.2 (GHSA-238p-pmpm-9mq7)

> Backing ADR: ADR-0089 Addendum A (curated `/lib/` toolkit; PWA converges on the toolkit pins, A6).
> Template: FRE-580 (commit 61c7c00c), the 0.16.11 → 0.16.47 migration.
> Tier: Standard (dependency override + a `src/` prompt string). One phase, one PR.

## Measured facts

- `katex@0.18.2` tarball and jsdelivr bytes give the same sha384 for `katex.min.js` and `katex.min.css`.
  - `katex.min.js`: `sha384-IHT6Yddb0KLCzZaxQ/so1j3xkJ8R6hOkCR06ma9NDHGn0Z4ClKcGtQ6qqsflRPxR`
  - `katex.min.css`: `sha384-qArEAQvOPs1o7K1iAybhs66g8nYTXVt4VLOQ3abu+OMLJyPNfaH4Wpko0X2epHJ+`
  - jsdelivr serves `access-control-allow-origin: *`.
- The `dist/fonts/` file names are identical in 0.16.47 and 0.18.2.
- 0.17.0 breaks the internal `__defineFunction` API. The PWA does not use it.
- 0.18.0 renames 21 internal CSS classes without an opt-in (`.base` → `.katex-base`, `.strut` → `.katex-strut`, and so on).
  No PWA or e2e selector uses a renamed class. The selectors in use (`katex`, `katex-display`, `katex-html`,
  `katex-mathml`, `mord`) remain.
- `mermaid@11.16.1` embeds its own KaTeX 0.16.45 chunk (`mermaid/dist/chunks/mermaid.esm/katex-FWVWKTT3.mjs`).
  The override cannot change it. `npm audit` does not flag it. A mermaid 12 upgrade removes it (separate work).
- `mermaid@11.16.1` (`katex ^0.16.45`), `rehype-katex@7.0.1` (`^0.16.0`) and `micromark-extension-math@3.1.0` (`^0.16.0`)
  declare ranges that exclude 0.18.2. A plain pin leaves nested vulnerable copies, so `npm audit` stays red.
  The fix is an npm `overrides` entry for `katex`.

## Steps

1. **Test first (RED).** In `seshat-pwa/src/__tests__/toolkit-convergence.test.ts`:
   change the pin assertion to `0.18.2`; add a test that `overrides.katex` equals the same pin.
   Run `cd seshat-pwa && npx vitest run src/__tests__/toolkit-convergence.test.ts`. Expect 2 failures.
2. **PWA (GREEN).** `seshat-pwa/package.json`: `katex` → `0.18.2` (exact) and `overrides.katex` → `0.18.2`.
   Run `cd seshat-pwa && npm install`. Expect `npm ls katex` to show only 0.18.2, every entry deduped.
3. **PWA cache.** `seshat-pwa/public/sw.js`: `CACHE_NAME` → `seshat-v68-fre-1720`.
4. **Toolkit surfaces** (`0.16.47` → `0.18.2`; SRI from the facts above):
   - `config/artifact_lib_manifest.json`
   - `config/artifact_lib_substitution_map.json` (lib_path, public_cdn_url, sri for js and css)
   - `src/personal_agent/tools/artifact_tools.py` (lines 1189-1190)
   - `src/personal_agent/observability/artifact_envelope/spec.py` (docstring)
   - `scripts/build_e2e_artifact_fixtures.py` (constants)
   - tests: `tests/observability/artifact_envelope/test_verifier.py`, `tests/scripts/test_build_e2e_artifact_fixtures.py`,
     `tests/personal_agent/storage/test_artifact_export.py`, `tests/personal_agent/service/test_artifact_egress.py`
   - docs: `docs/reference/PROMPT_CORPUS.md`, `docs/reference/artifact-builder/references/katex.md`,
     `docs/reference/artifact-builder/references/maintenance.md`, `e2e/artifact-lib/artifact-lib.spec.ts` (stale comment)
5. **Verify.**
   - `cd seshat-pwa && npm audit` → no katex advisory (AC-1).
   - `cd seshat-pwa && npx vitest run` → convergence and `MarkdownContent` math tests pass (AC-2).
   - `grep -rn "0\.16\.47"` outside plans and research → no hit.
   - `make test`, `make mypy`, `make ruff-check`, `make ruff-format`, `pre-commit run --all-files`, `cd seshat-pwa && npm run lint`.
   - Render `x^2` and `\htmlData` through 0.18.2 in node and check the PWA class names.
6. **Deploy (master).**
   - Upload `lib/katex@0.18.2/` (`katex.min.js`, `katex.min.css`, `fonts/`) to R2 before the gateway rebuild.
   - Check the bytes after the upload: fetch each file from the origin and compare its sha384 with the SRI above.
     `make verify-lib` reads headers only, so it cannot prove AC-3.
   - Rebuild the PWA (the shell changes).

## Acceptance criteria

| AC | Proof |
|----|-------|
| AC-1 | `npm audit` output in `seshat-pwa` shows no katex advisory |
| AC-2 | vitest summary: `toolkit-convergence.test.ts` and `MarkdownContent.test.tsx` pass |
| AC-3 | post-merge, master: `/lib/` serves the new hashes; the Dependabot alert closes |

## Out of scope

- The `/lib/` R2 upload and the Dependabot alert closure (master).
- A mermaid 12 upgrade (separate work; it also removes mermaid's embedded KaTeX 0.16.45 chunk).
