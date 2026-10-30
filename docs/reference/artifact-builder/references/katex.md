# KaTeX → math / formula typesetting (MIT)

Part of the artifact builder's source documents (`docs/reference/artifact-builder/`; it is not an agent skill). Reference each asset by its exact absolute, version-pinned URL. Source of truth: `config/artifact_lib_manifest.json`.

```html
<link rel="stylesheet" href="https://artifacts.example.com/lib/katex@0.18.2/katex.min.css">
<script src="https://artifacts.example.com/lib/katex@0.18.2/katex.min.js"></script>
```

```html
<span id="eq"></span>
<script>
  katex.render("c = \\pm\\sqrt{a^2 + b^2}", document.getElementById("eq"),
    { throwOnError: false, displayMode: true });
</script>
```

**Gotchas:**
- The **CSS `<link>` is mandatory** — without it KaTeX renders boxes/tofu (the glyph metrics
  and math fonts live in the stylesheet). Loading only the JS is the most common KaTeX failure.
- KaTeX's stylesheet resolves its math fonts from a **co-hosted `fonts/` directory relative to
  the CSS** — do not rewrite or relocate the CSS `url(fonts/…)` references; the shelf hosts them
  at the path the CSS expects.
- Use `throwOnError: false` so a single bad expression degrades to a readable error instead of
  aborting render. `displayMode: true` for block equations, omit for inline.
- KaTeX is **not** auto-render: it does not scan the page for `$…$` delimiters by default.
  Call `katex.render(tex, element, opts)` per equation (the auto-render extension is not on the
  shelf — render explicitly).
