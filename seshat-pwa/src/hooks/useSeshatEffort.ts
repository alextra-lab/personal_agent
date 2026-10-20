'use client';

import { useState, useEffect, useCallback, useRef } from 'react';

import { getSession, setSessionEffort } from '@/lib/agui-client';
import { DEFAULT_EFFORT, EFFORT_LEVELS } from '@/lib/effort';
import type { SeshatEffort } from '@/lib/types';

import type { ServerEffort } from './useAgentStream';

export interface UseSeshatEffortResult {
  /**
   * The level the selector shows. Null until the server has answered for this session
   * (the session GET or a `session_effort` STATE_DELTA), so the selector never shows a
   * cached or default level that could differ from the server's (ADR-0157 D2).
   */
  level: SeshatEffort | null;
  /** The level every chat request carries: the shown level, else Standard. */
  sendLevel: SeshatEffort;
  /**
   * False while the session GET is pending. A send in that window would carry Standard for a
   * session that may hold another level, so the composer holds its Send button until this is true.
   */
  ready: boolean;
  /** The message of the last failed write; null otherwise. */
  error: string | null;
  /** Ask the server to change the level. Returns to the stored level if the write fails. */
  change: (level: SeshatEffort) => void;
}

function isEffort(value: unknown): value is SeshatEffort {
  return EFFORT_LEVELS.includes(value as SeshatEffort);
}

/**
 * The Seshat effort level of one session (ADR-0157 D2).
 *
 * The server holds the level. This hook hydrates it from the session GET on mount and from the
 * `session_effort` STATE_DELTA (a change from elsewhere, or the replay on reconnect). A change is
 * one `PATCH`. The new level shows at once. If the write fails, the level returns to the last
 * level that the server confirmed, and `error` holds the reason (invariant 9).
 *
 * Writes go out one at a time, in the order of the choices, so the last choice is the last
 * commit. A session whose GET found no row yet shows Standard. A PATCH 404 on such a session
 * keeps the chosen level: the first chat request carries it and the server stores it. A 404 on
 * any other session is a failed write.
 *
 * @param sessionId    - The displayed session, or undefined before the route resolves.
 * @param serverEffort - The latest `session_effort` STATE_DELTA from the stream, or null.
 */
export function useSeshatEffort(
  sessionId: string | undefined,
  serverEffort: ServerEffort | null,
): UseSeshatEffortResult {
  const [level, setLevel] = useState<SeshatEffort | null>(null);
  const [ready, setReady] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // The last level the server confirmed for this session; null while unknown.
  const storedRef = useRef<SeshatEffort | null>(null);
  // True when the session GET found no row (a brand-new conversation).
  const noRowYetRef = useRef(false);
  // Bumped by a session change and by a server notice. An answer that began before the bump
  // describes an older state and must not apply.
  const epochRef = useRef(0);
  // Bumped by every choice. The latest choice alone may revert the selector.
  const writeSeqRef = useRef(0);
  // The tail of the write queue; it never rejects.
  const queueRef = useRef<Promise<void>>(Promise.resolve());

  // The session changed: forget the previous session's level and read the new one.
  useEffect(() => {
    const epoch = ++epochRef.current;
    const writeSeq = writeSeqRef.current;
    storedRef.current = null;
    noRowYetRef.current = false;
    setLevel(null);
    setError(null);
    setReady(false);
    if (!sessionId) return;

    getSession(sessionId)
      .then((session) => {
        // A server notice or a choice made while the GET was pending is newer than the GET.
        if (epochRef.current !== epoch || writeSeqRef.current !== writeSeq) return;
        noRowYetRef.current = session === null;
        const stored = isEffort(session?.seshat_effort) ? session.seshat_effort : DEFAULT_EFFORT;
        storedRef.current = stored;
        setLevel(stored);
      })
      .catch(() => {
        // The level stays unknown. Chat requests send Standard, and the server decides.
      })
      .finally(() => {
        if (epochRef.current === epoch) setReady(true);
      });
  }, [sessionId]);

  // The server says the level changed (another tab, or the replay on reconnect).
  useEffect(() => {
    if (!serverEffort || serverEffort.sessionId !== sessionId) return;
    epochRef.current += 1;
    noRowYetRef.current = false;
    storedRef.current = serverEffort.level;
    setLevel(serverEffort.level);
    setError(null);
    setReady(true);
  }, [serverEffort, sessionId]);

  const change = useCallback(
    (next: SeshatEffort) => {
      const epoch = epochRef.current;
      const writeSeq = ++writeSeqRef.current;
      setLevel(next);
      setError(null);
      if (!sessionId) return;

      const write = async (): Promise<void> => {
        try {
          await setSessionEffort(sessionId, next);
          if (epochRef.current === epoch) storedRef.current = next;
        } catch (err: unknown) {
          // A newer notice or a newer choice decides the display now.
          if (epochRef.current !== epoch || writeSeqRef.current !== writeSeq) return;
          // No row yet: the chosen level stays and rides the first message.
          if (noRowYetRef.current && err instanceof Error && err.message.endsWith(': 404')) return;
          setLevel(storedRef.current);
          setError(err instanceof Error ? err.message : 'Could not change the effort level');
        }
      };
      queueRef.current = queueRef.current.then(write);
    },
    [sessionId],
  );

  return { level, sendLevel: level ?? DEFAULT_EFFORT, ready, error, change };
}
