# artifact-builder — maintenance notes

For a person who changes the artifact builder source, the manifest or the generation prompt. The library sections are the other files of `references/`. The builder is not an agent skill (see `../SKILL.md`).

## Distillation Map (doc → generation prompt)

The generation prompt (`_HTML_GENERATION_SYSTEM_PROMPT`) is a compressed distillation of this
doc. Keep the pairs in sync when either changes:

| This doc section | Prompt block it feeds |
|---|---|
| The Curated `/lib/` Shelf and the library files of `references/` (the absolute URLs) | `CURATED TOOLKIT` bullet (exact `<script>`/`<link>` snippets) |
| Per-library "when to reach for each" | `CURATED TOOLKIT` per-asset one-liners |
| three.js textures-as-data-URI gotcha | `CURATED TOOLKIT` three.js note + `SEALED-BOX` no-network |
| paged.js experimental/gated | `CURATED TOOLKIT` book/print "experimental" line |
| Native Typography | `NATIVE TYPOGRAPHY` bullet |
| Design-System Conventions | `REQUIREMENTS` design-system bullets |
| Sealed-Box Constraints | `SEALED-BOX CONSTRAINTS` bullet |
| Portability (3 lanes) | `PORTABILITY` bullet |
| Never bake secrets | (carried as the standing secrets rule) |

## E2E verification

The done-bar for the curated toolkit is **"it renders under the live policy, and the
envelope is provably applied"** — not "it renders". Two complementary gates cover it:

**1. Hermetic render harness (in-repo, CI-able) — `make verify-artifact-e2e`.**
Builds a real, SRI-pinned fixture set (the repository's `build_e2e_artifact_fixtures.py`: KaTeX
0.16.47 + Chart.js 4.4.7 + paged.js fetched from the substitution-map CDN twins, each
byte-verified against the pinned `sha384`), then runs `e2e/artifact-lib/` on **Chromium
and WebKit**:

- **A — hosted render** under the *exact* artifact CSP directive set (served by a local
  CSP server whose header is emitted by the Python builder from `EXPECTED_CSP_DIRECTIVES`,
  the single source of truth). Asserts the KaTeX MathML annotation echoes the source TeX,
  the live `Chart.getChart()` instance holds the seeded dataset + the canvas painted, the
  CSP header is present, and **zero CSP violations**.
- **B — offline export** — the real `export_artifact_html(mode="inline")` output opened via
  `file://` with **all network aborted**; same semantic render assertions; proves zero
  network requests (truly self-contained).
- **C — paged.js eval-gate** — loads paged.js under the eval-free CSP and asserts it runs
  with **no `eval`/script CSP violation**.

One-time browser install: `cd e2e/artifact-lib && npx playwright install --with-deps webkit chromium`.

**Fidelity gap:** the harness rebinds the CSP host token to its localhost serving origin, so
it proves render under the directive *shape*, not the exact `artifacts.example.com` tokens.

**2. Live-origin gate (master, post-merge) — `make verify-envelope URL=…` + `make verify-lib`.**
Closes the host-token gap on the real Access-gated origin: the served artifact carries the
exact CSP directive set + MIME + `nosniff`, and every `/lib/` asset serves the correct
executable/typed MIME + `nosniff` reachable under the artifact CSP. Requires a CF Access
service token; runs at the deploy gate. A real-device iOS Safari pass is the owner's check.

**paged.js Scenario C result:** paged.js 0.4.3 runs **eval-free**
under the eval-free artifact CSP and paginates on **both Chromium and WebKit** in the hermetic
harness — corroborating the static-analysis claim. The `eval_gated` flag is **left set** (the
shelf entry stays *experimental/gated*); un-gating it — which would add paged.js to the default
`verify-lib` assert set — is a separate decision pending the live-origin confirmation.

## References

- ADR-0089 — Artifact Execution Security Model (`docs/architecture_decisions/ADR-0089-artifact-execution-security-model.md`), §D2/D3/D4 + **Addendum A** (A2 curated set, A3 hosting/pinning, A4 this doc, A5 export, A7 done-bar).
- `config/artifact_lib_manifest.json` — the single cross-repo source of truth for `/lib/` origin + paths + versions.
- `src/personal_agent/tools/artifact_tools.py` — `_HTML_GENERATION_SYSTEM_PROMPT` (the distilled runtime prompt) + `artifact_draft` tool.
- The `mermaid-diagrams` skill — sibling skill; the static-diagram portability lane.
