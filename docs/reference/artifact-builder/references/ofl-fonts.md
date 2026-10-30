# OFL fonts → publishing-grade prose (SIL OFL, variable woff2)

Part of the artifact builder's source documents (`docs/reference/artifact-builder/`; it is not an agent skill). Reference each asset by its exact absolute, version-pinned URL. Source of truth: `config/artifact_lib_manifest.json`.

Declare each face with `@font-face`, then wire it into the design-system custom properties:

```css
@font-face {
  font-family: "Source Serif 4";
  src: url("https://artifacts.example.com/lib/fonts/source-serif-4@4.005/source-serif-4.woff2") format("woff2");
  font-weight: 200 900; font-display: swap;
}
@font-face {
  font-family: "Playfair Display";
  src: url("https://artifacts.example.com/lib/fonts/playfair-display@2.103/playfair-display.woff2") format("woff2");
  font-weight: 400 900; font-display: swap;
}
@font-face {
  font-family: "JetBrains Mono";
  src: url("https://artifacts.example.com/lib/fonts/jetbrains-mono@2.304/jetbrains-mono.woff2") format("woff2");
  font-weight: 100 800; font-display: swap;
}
:root {
  --font-serif: "Source Serif 4", Georgia, serif;       /* body prose */
  --font-display: "Playfair Display", Georgia, serif;    /* display headings */
  --font-mono: "JetBrains Mono", ui-monospace, monospace;/* code */
}
```

**Gotchas:**
- These are **variable** woff2 fonts — declare a weight range (`font-weight: 200 900`) so the
  whole axis is usable from one file.
- `font-src https://artifacts.example.com data:` already admits them — **no CSP change**.
  Google Fonts / other font CDNs silently fail; use these or inline `data:` woff2.
- Use Source Serif 4 for body prose, Playfair Display for display headings, JetBrains Mono for
  code, falling back to system faces only when no shelf font fits.
