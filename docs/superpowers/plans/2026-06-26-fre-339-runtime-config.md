# FRE-339: Replace build-time env bake with runtime config

**Branch**: `fre-339-runtime-config`  
**Ticket**: [FRE-339](https://linear.app/frenchforest/issue/FRE-339)  
**Tier**: Tier-2:Sonnet

---

## Problem

`Dockerfile.pwa` lines 22–26 bake `NEXT_PUBLIC_SESHAT_URL` and `NEXT_PUBLIC_GATEWAY_TOKEN` into the
Next.js bundle at `docker build` time. Running the same image on the laptop mirror requires a different
ARG value, forcing a rebuild — defeating FRE-214 Track 2b's "one image, two deployments" goal.

## Approach

**Server Component → Client Component prop injection** (preferred over client-side fetch):

- `layout.tsx` (Server Component) reads `process.env.SESHAT_URL` and `process.env.GATEWAY_TOKEN`
  at request time (runtime, not build time) and passes them as props to `RuntimeConfigProvider`.
- `RuntimeConfigProvider` (Client Component) calls `initAguiConfig()` synchronously before
  children render, setting module-level vars in `agui-client.ts`.
- `agui-client.ts` changes `export const SESHAT_API` → `export let SESHAT_API` to allow
  reassignment from `initAguiConfig`, preserving all external import sites as live bindings.
- Also adds `GET /api/runtime-config` Next.js API route (debugging + future SW use).

No client-side fetch on the hot path. Config is propagated before any child renders.

## Why not a fetch?

The ticket recommended a backend-fetch approach, but reading env vars in a Server Component is
strictly better: zero extra round-trip, no loading state, no localStorage, same isolation guarantee.
The `/api/runtime-config` route is added as a debugging endpoint and for future use.

---

## Files

| Action | File | Notes |
|--------|------|-------|
| NEW | `seshat-pwa/src/app/api/runtime-config/route.ts` | Next.js Route Handler — reads `SESHAT_URL` / `GATEWAY_TOKEN` server-side, returns JSON |
| NEW | `seshat-pwa/src/components/RuntimeConfigProvider.tsx` | `'use client'` — calls `initAguiConfig(seshatUrl, gatewayToken)` inline before children |
| MODIFY | `seshat-pwa/src/app/layout.tsx` | Read `process.env.SESHAT_URL/GATEWAY_TOKEN`, wrap children in `<RuntimeConfigProvider>` |
| MODIFY | `seshat-pwa/src/lib/agui-client.ts` | `const SESHAT_API` → `let SESHAT_API = 'http://localhost:9000'`; add `initAguiConfig()`; `const GATEWAY_TOKEN` → `let GATEWAY_TOKEN = ''` |
| MODIFY | `Dockerfile.pwa` | Remove lines 22–26 (ARG/ENV for both NEXT_PUBLIC_ vars) |
| MODIFY | `docker-compose.cloud.yml` | Move pwa build.args → runtime `environment:` using `SESHAT_URL`/`GATEWAY_TOKEN` keys |
| MODIFY | `seshat-pwa/next.config.js` | Line 12: `NEXT_PUBLIC_SESHAT_URL` → `SESHAT_URL` (dev rewrites run in Node.js, not browser) |

---

## Atomic steps

### Step 1 — Write failing tests

Create `seshat-pwa/src/__tests__/RuntimeConfigProvider.test.tsx`:
- renders children after calling initAguiConfig with given seshatUrl + gatewayToken
- initAguiConfig is called before children render (not in an effect)

Create `seshat-pwa/src/__tests__/api-runtime-config.test.ts`:
- GET handler returns `{seshat_url, gateway_token}` from env
- Defaults to localhost when env vars absent

Verify: `cd seshat-pwa && npm run test` — new tests fail.

### Step 2 — Add `/api/runtime-config` route

Returns only `seshat_url` — **omit `gateway_token`** (bearer token must not be served
over an unauthenticated debug endpoint; it's already handled via the Server Component prop).

`seshat-pwa/src/app/api/runtime-config/route.ts`:
```ts
import { NextResponse } from 'next/server'

export function GET() {
  return NextResponse.json({
    seshat_url: process.env.SESHAT_URL ?? 'http://localhost:9000',
  })
}
```

### Step 3 — Add `RuntimeConfigProvider`

`seshat-pwa/src/components/RuntimeConfigProvider.tsx`:
```tsx
'use client'
import { useLayoutEffect } from 'react'
import { initAguiConfig } from '@/lib/agui-client'

interface RuntimeConfigProviderProps {
  seshatUrl: string
  gatewayToken: string
  children: React.ReactNode
}

export function RuntimeConfigProvider({ seshatUrl, gatewayToken, children }: RuntimeConfigProviderProps) {
  // useLayoutEffect fires after DOM commit but before browser paint,
  // and parent useLayoutEffect fires AFTER children useLayoutEffect but
  // BEFORE children useEffect — so all child API calls in useEffect see
  // the correct URL. Do NOT call initAguiConfig inline in render:
  // React 18 concurrent mode may discard render results without running effects.
  useLayoutEffect(() => {
    initAguiConfig(seshatUrl, gatewayToken)
  }, [seshatUrl, gatewayToken])

  return <>{children}</>
}
```

**Note**: `useLayoutEffect` is correct here. Parent `useLayoutEffect` fires before children's `useEffect` (where API calls live), so SESHAT_API is initialized before any fetch is attempted.

### Step 4 — Modify `agui-client.ts`

Replace lines 20–21:
```ts
export const SESHAT_API =
  process.env.NEXT_PUBLIC_SESHAT_URL ?? 'http://localhost:9000';
```
With:
```ts
export let SESHAT_API = 'http://localhost:9000';
```

Replace line 28:
```ts
const GATEWAY_TOKEN = process.env.NEXT_PUBLIC_GATEWAY_TOKEN ?? '';
```
With:
```ts
let GATEWAY_TOKEN = '';
```

Add after GATEWAY_TOKEN declaration:
```ts
export function initAguiConfig(seshatUrl: string, gatewayToken: string): void {
  SESHAT_API = seshatUrl;
  GATEWAY_TOKEN = gatewayToken;
}
```

Update docstring on SESHAT_API and GATEWAY_TOKEN (remove NEXT_PUBLIC_ references).

### Step 5 — Modify `layout.tsx`

`export const dynamic = 'force-dynamic'` is required: without it, Next.js may pre-render this route
at build time and bake `process.env.SESHAT_URL` from the BUILD environment (empty) rather than
reading it at request time from the RUNTIME environment. This would silently fall back to localhost
in production.

```tsx
import { RuntimeConfigProvider } from '@/components/RuntimeConfigProvider'

// Must be force-dynamic so process.env.SESHAT_URL/GATEWAY_TOKEN are read
// at runtime from the Node.js environment, not baked at build time.
export const dynamic = 'force-dynamic'

export default function RootLayout({ children }: RootLayoutProps) {
  const seshatUrl = process.env.SESHAT_URL ?? 'http://localhost:9000'
  const gatewayToken = process.env.GATEWAY_TOKEN ?? ''

  // Warn if SESHAT_URL is absent in production (silent fallback = mis-routed requests).
  if (process.env.NODE_ENV === 'production' && !process.env.SESHAT_URL) {
    console.error('[seshat-pwa] SESHAT_URL not set — defaulting to localhost:9000')
  }

  return (
    <html lang="en" className="dark h-full">
      <body className="h-full bg-slate-900 text-slate-100 antialiased">
        <RegisterSW />
        <RuntimeConfigProvider seshatUrl={seshatUrl} gatewayToken={gatewayToken}>
          {children}
        </RuntimeConfigProvider>
      </body>
    </html>
  )
}
```

### Step 6 — Modify `Dockerfile.pwa`

Remove lines 22–26 entirely (both ARG/ENV pairs for NEXT_PUBLIC_ vars).

### Step 7 — Modify `docker-compose.cloud.yml`

In the `seshat-pwa:` service:
- Remove `NEXT_PUBLIC_SESHAT_URL` and `NEXT_PUBLIC_GATEWAY_TOKEN` from `build.args:`
- Add under `environment:`:
  ```yaml
  environment:
    SESHAT_URL: "https://agent.example.com"
    GATEWAY_TOKEN: ${GATEWAY_TOKEN_PWA}
  ```

### Step 8 — Modify `next.config.js`

Line 12: `process.env.NEXT_PUBLIC_SESHAT_URL || 'http://localhost:9000'`  
→ `process.env.SESHAT_URL || 'http://localhost:9000'`

### Step 9 — Quality gates

```bash
cd seshat-pwa && npm run test  # all pass
cd seshat-pwa && npm run build # next build succeeds (no NEXT_PUBLIC_ baking)
```

---

## Acceptance criteria

| # | Criterion | Verify |
|---|-----------|--------|
| 1 | `Dockerfile.pwa` has no `ARG NEXT_PUBLIC_*` or `ENV NEXT_PUBLIC_*` | `grep NEXT_PUBLIC Dockerfile.pwa` → no match |
| 2 | `docker-compose.cloud.yml` pwa service uses runtime `environment:` not build `args:` | `grep -A5 'seshat-pwa:' docker-compose.cloud.yml` |
| 3 | `npm run test` passes | vitest exits 0 |
| 4 | `npm run build` succeeds | next build exits 0 |
| 5 | `initAguiConfig` exported and sets SESHAT_API + GATEWAY_TOKEN | new test coverage |

---

## External consumers of SESHAT_API (must not break)

- `seshat-pwa/src/lib/submitTurnRating.ts:16` — `import { SESHAT_API } from './agui-client'`
- `seshat-pwa/src/hooks/useInferenceStatus.ts:5` — `import { SESHAT_API } from '@/lib/agui-client'`

ES module live bindings propagate the reassignment automatically — no changes needed in these files.
Tests that mock `SESHAT_API` via `vi.mock` continue working unchanged.
