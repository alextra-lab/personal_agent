# highlight.js → code / syntax highlighting (BSD-3, global `hljs`)

Part of the artifact builder's source documents (`docs/reference/artifact-builder/`; it is not an agent skill). Reference each asset by its exact absolute, version-pinned URL. Source of truth: `config/artifact_lib_manifest.json`.

```html
<link rel="stylesheet" href="https://artifacts.example.com/lib/highlightjs@11.9.0/github-dark.min.css">
<script src="https://artifacts.example.com/lib/highlightjs@11.9.0/highlight.min.js"></script>
```

```html
<pre><code class="language-python">def greet(name): return f"hi {name}"</code></pre>
<script>
  document.addEventListener("DOMContentLoaded", () => hljs.highlightAll());
</script>
```

**Gotchas:**
- Put code inside `<pre><code class="language-XXX">…</code></pre>` and call
  `hljs.highlightAll()` **after the DOM is ready** (`DOMContentLoaded`) — calling it before the
  `<code>` elements exist highlights nothing.
- The theme **`<link>` carries the colors**; without it highlighted tokens get class names but
  no styling. `github-dark` is the hosted theme.
- HTML-escape `<`, `>`, `&` inside the code text, or the browser parses them as markup.
