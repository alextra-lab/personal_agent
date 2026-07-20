/**
 * ObserveView — resolved role bindings + provider table (ADR-0121 §5, FRE-920).
 *
 * The swappability principle made legible in the UI: which roles are pinned
 * (writers — never user-selectable) vs. open (the model picker governs
 * `primary` today), what each currently resolves to, and each provider's
 * placement + live health.
 *
 * MANUAL TEST PLAN
 * ================
 * 1. Navigate to /observe from the session drawer while a conversation is open.
 *    Expected: `primary` shows "open" with its resolved model + provenance;
 *    writer roles (entity_extraction, captains_log, embedding, reranker, …)
 *    show "pinned" with no candidates/provenance.
 * 2. Navigate to /observe directly (no session, e.g. a fresh tab) — falls back
 *    to the sessionless config; roles render without a resolved/provenance
 *    column (nothing to resolve without a session).
 * 3. Provider table shows placement (local/cloud), live availability dot, and
 *    max_concurrency for every provider in the catalog.
 * 4. "← Conversations" back link returns to the most recent session (/).
 */

'use client';

import { useSearchParams } from 'next/navigation';
import Link from 'next/link';

import { useSessionConfig } from '@/hooks/useSessionConfig';

const LAST_SESSION_KEY = 'seshat_last_session_id';

function ListSkeleton() {
  return (
    <div className="px-4 py-6 space-y-2">
      {[0, 1, 2, 3].map((i) => (
        <div key={i} className="h-10 rounded-lg bg-slate-800/60 animate-pulse" />
      ))}
    </div>
  );
}

export function ObserveView() {
  const searchParams = useSearchParams();
  const querySessionId = searchParams.get('session') ?? undefined;
  const lastSessionId =
    typeof window !== 'undefined' ? (localStorage.getItem(LAST_SESSION_KEY) ?? undefined) : undefined;
  const sessionId = querySessionId ?? lastSessionId;

  const { roles, providers, loading, hydrated } = useSessionConfig(sessionId);
  const roleEntries = Object.entries(roles);

  return (
    <div className="min-h-full bg-slate-900 text-slate-100 flex flex-col">
      {/* Header */}
      <header
        className="flex items-center gap-3 px-4 border-b border-slate-700 bg-slate-900/80 backdrop-blur-sm flex-shrink-0"
        style={{ paddingTop: 'calc(env(safe-area-inset-top, 0px) + 0.75rem)', paddingBottom: '0.75rem' }}
      >
        <Link
          href="/"
          className="flex items-center gap-1 text-sm text-slate-400 hover:text-slate-100 transition-colors"
        >
          ← Conversations
        </Link>
        <span className="text-slate-700">|</span>
        <h1 className="text-sm font-semibold text-slate-100">Observe</h1>
      </header>

      <main className="flex-1 overflow-y-auto px-4 py-4 space-y-6">
        {loading ? (
          <ListSkeleton />
        ) : (
          <>
            {!hydrated && (
              <p className="text-xs text-slate-500">
                No active conversation — showing catalog defaults, not a session&rsquo;s live selection.
              </p>
            )}

            {/* Role bindings */}
            <section>
              <h2 className="text-xs font-semibold uppercase tracking-wide text-slate-500 mb-2">
                Role bindings
              </h2>
              <div className="rounded-xl border border-slate-800 divide-y divide-slate-800 overflow-hidden">
                {roleEntries.map(([role, entry]) => (
                  <div key={role} className="flex items-center gap-3 px-3 py-2.5 text-sm">
                    <span
                      className={`text-[10px] font-semibold uppercase tracking-wide px-1.5 py-0.5 rounded flex-shrink-0 w-14 text-center ${
                        entry.open
                          ? 'bg-blue-900/40 text-blue-300'
                          : 'bg-slate-800 text-slate-500'
                      }`}
                    >
                      {entry.open ? 'Open' : 'Pinned'}
                    </span>
                    <span className="font-medium text-slate-100 w-36 flex-shrink-0 truncate">{role}</span>
                    {hydrated && entry.resolved ? (
                      <span className="text-slate-400 truncate">
                        {entry.resolved}
                        {entry.provenance && (
                          <span className="text-slate-600"> · {entry.provenance}</span>
                        )}
                      </span>
                    ) : (
                      <span className="text-slate-600">—</span>
                    )}
                  </div>
                ))}
                {roleEntries.length === 0 && (
                  <p className="px-3 py-4 text-xs text-slate-500">No roles configured.</p>
                )}
              </div>
            </section>

            {/* Providers */}
            <section>
              <h2 className="text-xs font-semibold uppercase tracking-wide text-slate-500 mb-2">
                Providers
              </h2>
              <div className="rounded-xl border border-slate-800 divide-y divide-slate-800 overflow-hidden">
                {providers.map((p) => (
                  <div key={p.key} className="flex items-center gap-3 px-3 py-2.5 text-sm">
                    <span
                      className={`w-2 h-2 rounded-full flex-shrink-0 ${
                        p.available ? 'bg-emerald-400' : 'bg-slate-600'
                      }`}
                      title={p.available ? 'Available' : 'Unavailable'}
                    />
                    <span className="font-medium text-slate-100 w-28 flex-shrink-0 truncate">{p.key}</span>
                    <span className="text-slate-500 w-16 flex-shrink-0">{p.placement}</span>
                    <span className="text-slate-600 flex-1 truncate hidden sm:block">{p.summary}</span>
                    <span className="text-slate-600 text-xs flex-shrink-0">
                      max {p.max_concurrency}
                    </span>
                  </div>
                ))}
                {providers.length === 0 && (
                  <p className="px-3 py-4 text-xs text-slate-500">No providers configured.</p>
                )}
              </div>
            </section>
          </>
        )}
      </main>
    </div>
  );
}
