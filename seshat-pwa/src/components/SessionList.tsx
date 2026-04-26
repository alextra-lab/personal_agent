'use client';

import { useEffect, useState } from 'react';
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

export function SessionList({ currentSessionId, onSelect }: SessionListProps) {
  const router = useRouter();
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);

    listSessions(20)
      .then((data) => {
        if (!cancelled) setSessions(data);
      })
      .catch(() => {
        if (!cancelled) setError('Could not load sessions.');
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });

    return () => { cancelled = true; };
  }, []); // Fetches once on open — parent re-mounts when drawer opens.

  const handleSelect = (sessionId: string) => {
    router.push(`/c/${sessionId}`);
    onSelect();
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center h-32 text-slate-500 text-sm">
        Loading…
      </div>
    );
  }

  if (error) {
    return (
      <div className="flex items-center justify-center h-32 text-red-400 text-sm">
        {error}
      </div>
    );
  }

  if (sessions.length === 0) {
    return (
      <div className="flex items-center justify-center h-32 text-slate-500 text-sm">
        No prior conversations.
      </div>
    );
  }

  return (
    <ul className="overflow-y-auto flex-1">
      {sessions.map((s) => {
        const isActive = s.session_id === currentSessionId;
        return (
          <li key={s.session_id}>
            <button
              onClick={() => handleSelect(s.session_id)}
              className={[
                'w-full text-left px-4 py-3 border-b border-slate-800 hover:bg-slate-800/50 transition-colors',
                isActive ? 'bg-slate-800/70' : '',
              ].join(' ')}
            >
              <p className={['text-sm font-medium truncate', isActive ? 'text-slate-100' : 'text-slate-300'].join(' ')}>
                {s.title ?? '(empty session)'}
              </p>
              <p className="text-xs text-slate-500 mt-0.5">
                {formatRelativeTime(s.last_active_at)} · {s.message_count} {s.message_count === 1 ? 'msg' : 'msgs'}
              </p>
            </button>
          </li>
        );
      })}
    </ul>
  );
}
