# Chart.js → charts / data visualization (MIT, global `Chart`)

Part of the artifact builder's source documents (`docs/reference/artifact-builder/`; it is not an agent skill). Reference each asset by its exact absolute, version-pinned URL. Source of truth: `config/artifact_lib_manifest.json`.

```html
<script src="https://artifacts.example.com/lib/chartjs@4.4.7/chart.umd.js"></script>
```

```html
<canvas id="spend" width="640" height="320"></canvas>
<script>
  new Chart(document.getElementById("spend"), {
    type: "bar",
    data: {
      labels: ["Q1", "Q2", "Q3", "Q4"],
      datasets: [{ label: "Spend", data: [12, 19, 8, 15] }]   // inline the data
    },
    options: { responsive: true, maintainAspectRatio: false }
  });
</script>
```

**Gotchas:**
- Give the `<canvas>` **explicit `width` and `height` attributes** (e.g.
  `<canvas width="640" height="320">`). Chart.js reads the element's attributes for initial
  layout; a CSS-sized parent alone is not enough and an unsized canvas collapses to 0×0 or
  renders blurry. If you want it fluid, wrap the canvas in a sized container **and** set
  `maintainAspectRatio: false`.
- **Inline the data** as a JS array/object literal. There is no `fetch` — never point Chart.js
  at a URL or load CSV/JSON over the network.
- The global is `Chart` (UMD build). One `new Chart(...)` per canvas.
