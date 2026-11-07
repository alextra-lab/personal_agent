'use client';

import { useEffect, useState } from 'react';
import type { ReactNode } from 'react';
import { useRouter } from 'next/navigation';

import { listSessions } from '@/lib/agui-client';
import type { SessionSummary } from '@/lib/agui-client';

interface SessionListProps {
  currentSessionId?: string;
  /** Called after the user selects a session (so the parent can close the drawer). */
  onSelect: () => void;
}

function formatRelativeTime(isoString: string): string {
  const diff = Date.now() - new Date(isoString).getTime();
  const minutes = Math.floor(diff / 60_000);
  if (minutes < 1) return 'just now';
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  return `${days}d ago`;
}

/** Sessions listed when the search box is empty. */
const RECENT_LIMIT = 20;
/** Sessions listed for a search (FRE-1746). */
const SEARCH_LIMIT = 50;
/** Pause in typing, in milliseconds, before a search request goes out. */
const SEARCH_DEBOUNCE_MS = 300;

export function SessionList({ currentSessionId, onSelect }: SessionListProps) {
  const router = useRouter();
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState('');
  const term = query.trim();

  // Runs on open (parent re-mounts when drawer opens) and again when the search text changes.
  // An empty box lists the recent sessions at once. Search text waits for a pause in typing.
  // The cleanup drops every update of a request that a newer one replaced.
  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);

    const load = () =>
      (term ? listSessions(SEARCH_LIMIT, term) : listSessions(RECENT_LIMIT))
        .then((data) => {
          if (!cancelled) setSessions(data);
        })
        .catch(() => {
          if (!cancelled) setError('Could not load sessions.');
        })
        .finally(() => {
          if (!cancelled) setLoading(false);
        });

    const timer = term ? setTimeout(load, SEARCH_DEBOUNCE_MS) : undefined;
    if (!term) void load();

    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [term]);

  const handleSelect = (sessionId: string) => {
    router.push(`/c/${sessionId}`);
    onSelect();
  };

  let results: ReactNode;
  if (loading) {
    results = (
      <div className="flex items-center justify-center h-32 text-ink-muted text-sm">
        Loading…
      </div>
    );
  } else if (error) {
    results = (
      <div className="flex items-center justify-center h-32 text-red-400 text-sm">
        {error}
      </div>
    );
  } else if (sessions.length === 0) {
    results = (
      <div className="flex items-center justify-center h-32 text-ink-muted text-sm">
        {term ? `No conversations match “${term}”.` : 'No prior conversations.'}
      </div>
    );
  } else {
    results = (
      <ul className="overflow-y-auto flex-1">
        {sessions.map((s) => {
          const isActive = s.session_id === currentSessionId;
          return (
            <li key={s.session_id}>
              <button
                onClick={() => handleSelect(s.session_id)}
                className={[
                  'block w-full text-left px-4 py-3 border-b border-line hover:bg-surface transition-colors',
                  isActive ? 'bg-surface' : '',
                ].join(' ')}
              >
                <p className={['text-sm font-medium truncate', isActive ? 'text-ink' : 'text-ink-muted'].join(' ')}>
                  {s.session_label ?? s.title ?? '(empty session)'}
                  {s.channel === 'EVAL' && (
                    <span className="ml-1.5 text-[10px] font-mono text-sky-400/70 border border-sky-400/30 rounded px-1">
                      EVAL
                    </span>
                  )}
                </p>
                <p className="text-xs text-ink-muted mt-0.5">
                  {formatRelativeTime(s.last_active_at)} · {s.turn_count ?? 0} {(s.turn_count ?? 0) === 1 ? 'turn' : 'turns'}
                </p>
                {s.session_digest && (
                  <p className="text-xs text-ink-muted mt-1 line-clamp-3">
                    {s.session_digest}
                  </p>
                )}
              </button>
            </li>
          );
        })}
        </ul>
    );
  }

  // The search box sits outside every state branch, so it stays mounted (and focused) while
  // results load.
  return (
    <div className="flex flex-col flex-1 min-h-0">
      <div className="px-4 py-2 border-b border-line">
        <input
          type="search"
          aria-label="Search conversations"
          placeholder="Search conversations"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          className="w-full rounded-md border border-line bg-surface px-3 py-1.5 text-base text-ink placeholder:text-ink-muted focus:outline-none focus:ring-1 focus:ring-line"
        />
      </div>
      {results}
    </div>
  );
}
