# FRE-938 — Session continuity: server fallback when the localStorage key is missing

**Linear**: [FRE-938](https://linear.app/frenchforest/issue/FRE-938/session-continuity-is-localstorage-only-with-no-server) (Approved · Medium · Sonnet)
**Branch**: `fre-938-session-continuity-server-fallback` (off `main`)
**Project**: Configuration Management (ADR-0121 family)

## Root cause (from the ticket)

The client's notion of "which conversation am I in" lives solely in the
`seshat_last_session_id` localStorage key, written only on send and on
starting a new conversation (`StreamingChat.tsx`). Nothing ever asks the
server when the key is absent (fresh device, new tab, cleared storage).

Two surfaces degrade unsafely:
- **Observe page** (`ObserveView.tsx`) calls the sessionless config endpoint,
  which never returns a `resolved` deployment — every role renders `—`.
- **Root route** (`page.tsx`) mints a brand-new session id instead of asking
  the server, silently orphaning the user's real history. Its docstring
  claims the opposite of what it does.

## Scope — three parts, smallest first

### Part 1 — sessionless config endpoint returns a resolved default

`src/personal_agent/gateway/session_api.py`, `get_config` (`@config_router.get("/config")`,
~L586-634): call the existing pure helper `_resolve_role_binding(role, config, None)`
(L351-396) for every role and add `resolved`/`provenance` to each entry —
identical to what `get_session_config` already does when a role has no
stored selection. Since both endpoints route through the same helper with
`stored=None`, they agree on the default by construction (this is exactly
what AC-2 asserts).

Update the two docstrings that currently claim the sessionless payload omits
`resolved`/`provenance` (`get_config` L590-607, and the note inside
`get_session_config` L505-509) — they no longer do.

### Part 2 — Observe page asks the server before assuming "no session"

`seshat-pwa/src/components/ObserveView.tsx`: when there is no `?session=`
query param **and** no localStorage key, ask the server for the most recent
session before falling back to the sessionless config. When the key or query
param **is** present, behavior (and network calls) must not change — AC-5.

**Implementation hazard (codex plan-review flagged this as the top risk —
read before coding):** `sessionId` state must be *seeded synchronously* from
`querySessionId ?? localStorage.getItem(LAST_SESSION_KEY)` in the `useState`
initializer, exactly as the current code does at L45-47. Do **not** initialize
it to `undefined` and resolve everything (including the already-present case)
inside a `useEffect` — that would call `useSessionConfig(undefined)` on the
first render even when localStorage already has a valid key, firing an extra
sessionless `GET /api/v1/config` and violating AC-5. The `useEffect` that
calls `resolveLastSessionId()`'s server lookup must be gated so it only runs
when the synchronously-seeded value is absent:
```ts
useEffect(() => {
  if (sessionId) return; // key or query already present — no network call (AC-5)
  let cancelled = false;
  resolveLastSessionId().then((id) => {
    if (!cancelled && id) setSessionId(id);
  });
  return () => { cancelled = true; };
}, [sessionId]);
```
Accepted UX tradeoff: in the both-absent case, this still causes one visible
transition — the sessionless (catalog-default) config renders first, then
upgrades to the session-scoped config once the server lookup resolves. This
is a real (not stale) render, not a bug, and is acceptable: AC-3 requires the
final state show the live selection, not that there be zero transition.

Also: since Part 1 makes the sessionless config return `resolved` for every
role, the render currently gates on `hydrated && entry.resolved` (L98) —
change to `entry.resolved` so the catalog-default resolution actually shows
(AC-3). `hydrated` still gates the "showing catalog defaults" banner text,
unchanged.

### Part 3 — root route does the same before minting a new id

`seshat-pwa/src/app/page.tsx`: when the localStorage key is missing, ask the
server for the most recent session before minting a new UUID. Mint only when
the user genuinely has no sessions (AC-4). Fix the docstring, which currently
asserts resume-from-storage always works.

### Shared helper (new, small)

Both Part 2 and Part 3 need "read localStorage; if absent, ask the server for
the most recent session" — the same fallback logic. Rather than duplicate it,
add one small helper:

`seshat-pwa/src/lib/session.ts` (new file):
```ts
import { listSessions } from './agui-client';

export const LAST_SESSION_KEY = 'seshat_last_session_id';

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export function isValidUUID(s: string): boolean {
  return UUID_RE.test(s);
}

/**
 * Resolve the session a returning visitor should land on.
 *
 * Checks localStorage first — no network call, the common case (AC-5).
 * Only when that key is absent or invalid does it ask the server for the
 * most recent session, so a cleared-storage visitor with existing history
 * resumes there instead of losing the reference (FRE-938).
 *
 * @returns The resolved session ID, or `undefined` when there is genuinely
 *   no session to resume (a brand-new user, or the lookup failed).
 */
export async function resolveLastSessionId(): Promise<string | undefined> {
  const stored = localStorage.getItem(LAST_SESSION_KEY);
  if (stored && isValidUUID(stored)) return stored;

  try {
    const sessions = await listSessions(1);
    return sessions[0]?.session_id;
  } catch {
    return undefined;
  }
}
```

This does **not** change `StreamingChat.tsx`'s write *behavior* — the two
write sites are explicitly out of scope and stay exactly as-is. The one
change there (codex plan-review flagged the drift risk of three independent
copies of the same string literal): replace its local
`const LAST_SESSION_KEY = 'seshat_last_session_id';` with
`import { LAST_SESSION_KEY } from '@/lib/session';` — same value, same
behavior, just one source of truth for the key name.

`seshat-pwa/src/lib/types.ts`: update `SessionConfigRole.resolved`/`provenance`
doc comments — no longer "Present only on the session-scoped read" now that
Part 1 ships; both endpoints return them, sessionless just always reports
`"default"` provenance (never `"server-hydrated"`).

## Out of scope (per ticket)

- Do NOT change `get_session_config` (session-scoped endpoint) — correct today.
- Do NOT change the two localStorage *write* sites in `StreamingChat.tsx`.
- Do NOT touch `useSessionConfig.ts`'s contract — `hydrated` semantics stay
  (session-scoped hydration confirmed vs. sessionless fallback), it just no
  longer gates whether a resolved value is *shown*.

## Files touched

- `src/personal_agent/gateway/session_api.py` — `get_config` + 2 docstrings
- `tests/personal_agent/gateway/test_session_api.py` — new tests
- `seshat-pwa/src/lib/session.ts` — new shared helper
- `seshat-pwa/src/app/page.tsx` — use the helper, fix docstring
- `seshat-pwa/src/components/ObserveView.tsx` — use the helper, fix render gate
- `seshat-pwa/src/components/StreamingChat.tsx` — one-line: import the shared
  `LAST_SESSION_KEY` constant instead of its own local copy (no behavior change)
- `seshat-pwa/src/lib/types.ts` — doc-comment fix only (no shape change)
- `seshat-pwa/src/__tests__/session.test.ts` — new
- `seshat-pwa/src/__tests__/page.test.tsx` — new
- `seshat-pwa/src/__tests__/ObserveView.test.tsx` — extend

## Acceptance criteria → proof plan

| # | Criterion | Test |
|---|-----------|------|
| AC-1 | Sessionless config returns `resolved` for every role (open + pinned), `provenance == "default"` | `test_get_config_returns_resolved_for_every_role` — asserts across the full declared role set, including a pinned role |
| AC-2 | Resolved value for a role with no stored selection == session-scoped endpoint's value for a session with no selection for that role | `test_get_config_resolved_matches_session_scoped_default` — calls both endpoints (session-scoped with empty selection store) and asserts each role's `resolved` value is equal (per-role comparison — codex plan-review confirmed this holds because both endpoints route through `_resolve_role_binding(role, config, None)`; the claim is scoped to the `resolved` field per role, not full-payload equality, since the session-scoped response also carries `session_id`) |
| AC-3 | Observe page renders a model name (not `—`) for every role with the key absent and no query param; still shows live selection when a conversation is active | `ObserveView.test.tsx` — updated "no session, no server session" case asserts no em-dash + a model name renders; new "no session but server has one" case asserts it upgrades to the session's live `resolved` value |
| AC-4 | Root route with key absent + existing sessions navigates to the most recent one; a user with no sessions gets a new one | `page.test.tsx` — two cases: `listSessions` returns a session → navigates there; returns `[]` → mints + navigates to a fresh UUID |
| AC-5 | No blocking network call added on the path where the key is present | `page.test.tsx` + `ObserveView.test.tsx` — assert `listSessions` is NOT called when the localStorage key (or `?session=`) is present |

## TDD steps

1. Backend: write `test_get_config_returns_resolved_for_every_role` and
   `test_get_config_resolved_matches_session_scoped_default` in
   `tests/personal_agent/gateway/test_session_api.py` (new section mirroring
   the existing `GET /{id}/config` section, reusing `_ALL_PROVIDERS_UP` /
   `_CHECK_ALL_PROVIDERS` fixtures) → confirm both fail against current
   `get_config` (no `resolved` key) → implement Part 1 → green.
2. Frontend helper: write `seshat-pwa/src/__tests__/session.test.ts`
   (mock `@/lib/agui-client`'s `listSessions`) covering: returns stored value
   without calling `listSessions`; calls `listSessions(1)` and returns its
   result when the key is absent; returns `undefined` on empty list or a
   thrown error → confirm fails (file doesn't exist) → implement
   `lib/session.ts` → green.
3. Root route: write `seshat-pwa/src/__tests__/page.test.tsx` (mock
   `@/lib/session`'s `resolveLastSessionId` and `next/navigation`'s
   `useRouter`) covering AC-4 both cases + AC-5 → confirm fails → implement
   Part 3 → green.
4. Observe page: extend `ObserveView.test.tsx` per the AC-3/AC-5 table above
   → confirm new/updated assertions fail against current code → implement
   Part 2 → green.
5. Full suite: `make test-file FILE=tests/personal_agent/gateway/test_session_api.py`,
   then `cd seshat-pwa && npm test`, then full `make test` + `npm run lint`.

## Quality gates

`make test` (targeted file, then full) · `make mypy` · `make ruff-check` +
`make ruff-format` · `pre-commit run --all-files` · `cd seshat-pwa && npm run lint`.

Self-review: `low`-effort code-review pass (small, localized diff — one
endpoint, one new pure helper, two call sites using it) plus a docs fix; no
security-review trigger (no new inputs/subprocess/auth/network-surface beyond
an existing, already-authenticated `listSessions` call).

## Deploy (per ticket)

Gateway rebuild for the endpoint change; PWA rebuild with a cache-name bump
for the client changes. Both ask-first, per lifecycle rules.
