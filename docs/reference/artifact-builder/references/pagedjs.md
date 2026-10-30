# paged.js → book / print layout (MIT) — **experimental, gated**

Part of the artifact builder's source documents (`docs/reference/artifact-builder/`; it is not an agent skill). Reference each asset by its exact absolute, version-pinned URL. Source of truth: `config/artifact_lib_manifest.json`.

paged.js is on the shelf **but is `eval_gated`**: it may require `eval` under the live CSP
(which omits `'unsafe-eval'`), pending runtime confirmation. Treat it as an
**advanced, experimental** option that **may silently fail under the live page policy** — do
**not** rely on it as a first-class library. For book/print layout, **lead with the native
`@page` / `column-count` / `break-*` recipes of Native Typography in `SKILL.md`**; reach for paged.js only for
hand-roll-impossible pagination (running heads, footnote placement, generated page numbers) and
only after confirming it loads in the target environment.
