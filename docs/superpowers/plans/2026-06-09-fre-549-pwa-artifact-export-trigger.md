# FRE-549 — PWA artifact export trigger (wire FRE-530 `/export` to an Export control)

**Ticket:** FRE-549 (Approved, Tier-2:Sonnet) · Project: Artifact Execution Security
**Refs:** FRE-530 (endpoint, live) · FRE-531 (CF token / inline prerequisite) · ADR-0089 Addendum A5

## Problem

FRE-530 shipped `GET /api/v1/artifacts/{id}/export?mode=inline|substitute`
(`src/personal_agent/service/artifacts_router.py:401`), but no PWA surface calls
it. The capability is live and unreachable. Add an **Export ▾** control to the
artifact drawer that triggers an in-session download.

## Endpoint contract (verified from source)

- Modes: `inline` (default, offline-portable) · `substitute` (CDN+SRI, online).
- Success: `200` with `Content-Disposition: attachment; filename="{slug}.html"`,
  `media_type=text/html`.
- Failures: `400` (not HTML), `401` (no CF Access), `404` (not found/other user),
  `502` (asset fetch / SRI failure — inline mode before the FRE-531 token lands),
  `503` (substrate not configured). FastAPI emits `422` only for an invalid `mode`
  query value — the UI never sends one, so it is not a reachable UI state.
- Auth: same as `listArtifacts` — CF Access JWT injected by the CF edge; PWA
  fetch carries `authHeaders()`. No new auth wiring.

## Design

- **Only HTML artifacts** (`contentType` starts with `text/html`) get the control;
  the endpoint 400s otherwise, so hide it.
- Small dropdown `Export ▾`, not two loud buttons. Two items:
  - **Offline (portable)** → `mode=inline` *(default / first item)*
  - **Online (lean)** → `mode=substitute`
- `502` → friendly "Offline export unavailable — try Online (lean)." Other
  non-2xx → generic "Export failed (status N)." Inline error text under the menu,
  never a crash.
- Download via `fetch → blob → object URL → anchor[download] → click → revoke`,
  so error codes are observable (a bare `<a href>`/navigation could not branch on
  502).

## Files

### 1. `seshat-pwa/src/lib/agui-client.ts` (add, end of Artifact helpers §)
- `export type ArtifactExportMode = 'inline' | 'substitute';`
- `export class ArtifactExportError extends Error` with `readonly status: number`.
- `export async function fetchArtifactExport(artifactId, mode): Promise<Blob>` —
  `fetch(${SESHAT_API}/api/v1/artifacts/{id}/export?mode={mode}, { headers: authHeaders() })`;
  throw `ArtifactExportError(resp.status, …)` on non-ok; else `resp.blob()`.

### 2. `seshat-pwa/src/components/ArtifactExportMenu.tsx` (new)
- Props: `artifactId: string`, `filename: string`.
- State: `open`, `busy`, `error: string | null`.
- `handleExport(mode)`: set busy; `const blob = await fetchArtifactExport(...)`;
  `triggerDownload(blob, filename)`; clear menu. On `ArtifactExportError` map
  `502`→friendly, else generic; clear busy in `finally`.
- `triggerDownload`: module-local helper using `URL.createObjectURL`, a temporary
  anchor with `download`, `.click()`, then `URL.revokeObjectURL`. Guarded by
  `typeof document !== 'undefined'`.
- Google-style doc comment; closes menu on outside interaction via a backdrop
  button (match existing menu idiom; no new deps).

### 3. `seshat-pwa/src/components/ArtifactViewer.tsx`
- Import `ArtifactExportMenu`.
- Compute `const isHtml = contentType.toLowerCase().startsWith('text/html');`
- Render `{isHtml && <ArtifactExportMenu artifactId={artifactId} filename={...} />}`
  in the header, before the "Open ↗" link. `filename` = `${title || 'artifact'}.html`
  sanitised, falling back to artifactId (server's Content-Disposition is
  authoritative; this is the client-suggested name only).

### 4. `seshat-pwa/public/sw.js`
- Bump `CACHE_NAME` `seshat-v21-toolkit-convergence` → `seshat-v22-artifact-export`
  (shell changes — new component in the bundle).

### 5. `seshat-pwa/src/__tests__/ArtifactExportMenu.test.tsx` (new, vitest)
- `vi.mock('@/lib/agui-client')` exposing `fetchArtifactExport`,
  `ArtifactExportError` (real class).
- Stub `URL.createObjectURL`/`revokeObjectURL`.
- Cases:
  1. Menu opens; both items rendered.
  2. Click **Offline** → `fetchArtifactExport(id,'inline')` once + `createObjectURL` called + **`revokeObjectURL` called** (codex gap: object-URL cleanup is not browser-automatic).
  3. Click **Online** → `fetchArtifactExport(id,'substitute')` once.
  4. `502` rejection → friendly message shown, no throw.
  5. Generic non-2xx (`503`) → generic message.
  6. Anchor `download` attribute carries the sanitised filename (codex gap: filename fallback/sanitisation regresses easily).

### 6. `seshat-pwa/src/__tests__/agui-client.export.test.ts` (new, vitest — codex gap)
- Stub `global.fetch`. Assert `fetchArtifactExport`:
  - returns the blob on `200`;
  - throws `ArtifactExportError` with `status === 400` on a `400` response
    (endpoint contract branch — distinct from `502`);
  - throws `ArtifactExportError` with `status === 502` on a `502`;
  - sends the request to `…/export?mode=inline` with `authHeaders()` and **no**
    `credentials` field (matches the existing fetch pattern; CF Access JWT is
    edge-injected).

### 7. `seshat-pwa/src/__tests__/ArtifactViewer.test.tsx` (update)
- Extend the `agui-client` mock to include `fetchArtifactExport` (component now
  imports it).
- Add: Export control present for HTML **and the existing "Open ↗" link + its
  `postCardClick` still work** (codex gap: verify coexistence in the real drawer,
  not the menu in isolation); Export control absent for a non-HTML contentType
  (e.g. `application/json`).

## TDD order
1. Write `ArtifactExportMenu.test.tsx` → `npx vitest run src/__tests__/ArtifactExportMenu.test.tsx` fails (no component).
2. Add `fetchArtifactExport` + component → test passes.
3. Wire into `ArtifactViewer`, update its test → passes.
4. Bump SW cache.

## Verify (exact)
```bash
cd seshat-pwa
npx vitest run src/__tests__/ArtifactExportMenu.test.tsx src/__tests__/ArtifactViewer.test.tsx
npm run test          # full vitest suite green
npx tsc --noEmit      # types
npm run lint
```
Expected: all green; tsc/lint clean.

## Out of scope
- "Save work produced inside a running artifact" (D4a parent-broker) — separate, unfiled.
- Enabling inline end-to-end depends on the FRE-531 CF service token; until then
  inline `502`s and the UI degrades gracefully to the friendly message. Substitute
  works today.
```
