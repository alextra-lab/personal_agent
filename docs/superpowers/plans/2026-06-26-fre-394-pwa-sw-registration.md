# FRE-394: PWA Service Worker Registration

**Branch:** `fre-394-pwa-sw-registration`
**Ticket:** [FRE-394](https://linear.app/frenchforest/issue/FRE-394)
**ADR:** None (no new architecture decision needed — this is wiring existing dead code)

---

## Context

`seshat-pwa/public/sw.js` has the correct network-first strategy with `skipWaiting()` and
`clients.claim()`, but nothing has ever registered it. The CACHE_NAME bump ritual is a no-op.
iOS PWA users see stale shells after deploys because the SW is never activated.

## What we're building

A 20-line `RegisterSW` client component mounted in `RootLayout`. No third-party libs needed.

## Acceptance Criteria

| # | Criterion |
|---|-----------|
| 1 | `/sw.js` is registered on load (client component in layout) |
| 2 | A new deploy triggers the new SW and the app updates without a manual hard-reload |
| 3 | Offline boot still works (network-first falls back to cache) |
| 4 | `CACHE_NAME` bump now actually evicts old caches |
| 5 | Update the SW-cache convention memory note |

## Atomic Steps

### Step 1 — Write failing test
File: `seshat-pwa/src/__tests__/RegisterSW.test.tsx`

Tests:
- `register('/sw.js')` is called on mount
- `controllerchange` triggers `window.location.reload()` when `wasControlled=true`
- `controllerchange` does NOT reload when `wasControlled=false` (first install)
- Graceful no-op when `navigator.serviceWorker` is absent

Run: `cd seshat-pwa && npx vitest run src/__tests__/RegisterSW.test.tsx`
Expected: all tests FAIL (component doesn't exist yet)

### Step 2 — Create RegisterSW component
File: `seshat-pwa/src/components/RegisterSW.tsx`

```tsx
'use client';

import { useEffect } from 'react';

/**
 * Registers /sw.js and auto-reloads when a new SW takes control.
 *
 * Guards:
 *   - SSR: useEffect only runs in browser
 *   - No SW support: early return
 *   - First install: captures wasControlled before register; skips reload when false
 *     (avoids an immediate reload on first-ever SW install)
 */
export function RegisterSW(): null {
  useEffect(() => {
    if (!('serviceWorker' in navigator)) return;

    const wasControlled = Boolean(navigator.serviceWorker.controller);
    let reloading = false;

    navigator.serviceWorker.register('/sw.js').catch(() => {
      // Registration failures are silent — the app still works without a SW.
    });

    navigator.serviceWorker.addEventListener('controllerchange', () => {
      if (reloading || !wasControlled) return;
      reloading = true;
      window.location.reload();
    });
  }, []);

  return null;
}
```

### Step 3 — Mount in layout.tsx
File: `seshat-pwa/src/app/layout.tsx`

Add `import { RegisterSW } from '@/components/RegisterSW';` and mount `<RegisterSW />` inside the `<body>`.

### Step 4 — Run tests
`cd seshat-pwa && npx vitest run src/__tests__/RegisterSW.test.tsx`
Expected: all tests pass.

### Step 5 — Update MEMORY note
Update the `project_pwa_sw_cache_convention.md` memory entry to reflect that registration now exists.

### Step 6 — Quality gates
```bash
cd seshat-pwa && npx vitest run
cd seshat-pwa && npx tsc --noEmit
```

## Key Design Decision: wasControlled guard

`clients.claim()` in the SW's activate handler causes `controllerchange` to fire even on first install
(no previous controller → new controller). Without the `wasControlled` guard, opening the PWA for the
first time would cause an immediate reload loop.

Captured BEFORE `register()` so it reflects the state at the time of component mount, not after
registration resolves.

## Out of Scope

- `visibilitychange` + `registration.update()` polling (iOS background resilience — FRE-236)
- Any changes to `sw.js` itself (it's already correct)
- next-pwa / Serwist tooling (unnecessary overhead for a custom SW this simple)
