# three.js → 3-D / spatial / physics (MIT, global `THREE`, r171)

Part of the artifact builder's source documents (`docs/reference/artifact-builder/`; it is not an agent skill). Reference each asset by its exact absolute, version-pinned URL. Source of truth: `config/artifact_lib_manifest.json`.

```html
<script src="https://artifacts.example.com/lib/three@0.171.0/three.iife.min.js"></script>
```

```html
<div id="stage" style="width:100%;height:420px"></div>
<script>
  const el = document.getElementById("stage");
  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(60, el.clientWidth / el.clientHeight, 0.1, 100);
  camera.position.z = 4;
  const renderer = new THREE.WebGLRenderer({ antialias: true });
  renderer.setSize(el.clientWidth, el.clientHeight);
  el.appendChild(renderer.domElement);

  const mesh = new THREE.Mesh(
    new THREE.IcosahedronGeometry(1, 0),
    new THREE.MeshStandardMaterial({ color: 0x6366f1, flatShading: true })
  );
  scene.add(mesh);
  scene.add(new THREE.DirectionalLight(0xffffff, 1).translateZ(5));
  scene.add(new THREE.AmbientLight(0x404040));

  (function loop() {
    requestAnimationFrame(loop);
    mesh.rotation.x += 0.005; mesh.rotation.y += 0.008;
    renderer.render(scene, camera);
  })();
</script>
```

**Gotchas:**
- **Textures and models must be `data:` URIs or generated in code — never fetched.**
  `connect-src 'none'` blocks `TextureLoader`/`GLTFLoader` over the network. Build geometry
  procedurally, embed textures as base64 `data:` URIs, or draw to an offscreen `<canvas>` and
  use it as a `CanvasTexture`.
- The shelf hosts a **bundled IIFE/global build** exposing `THREE` on `window` — there is no
  ES-module import (the sealed null-origin can't resolve `import`/CORS). Use the global, not
  `import * as THREE`.
- Size the renderer to a container with an explicit height; on resize, update
  `camera.aspect` + `renderer.setSize`. Dispose geometries/materials/renderer if you tear a
  scene down to avoid GPU leaks in long-lived artifacts.
