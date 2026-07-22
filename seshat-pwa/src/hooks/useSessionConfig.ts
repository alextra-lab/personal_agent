'use client';

import { useState, useEffect, useCallback, useRef } from 'react';

import { getConfig, getSessionConfig } from '@/lib/agui-client';
import type { SessionConfig } from '@/lib/types';

const POLL_INTERVAL_MS = 60_000;

export interface UseSessionConfigResult {
  /** Per-role config (open/pinned, resolved binding, candidates). Empty until first load. */
  roles: SessionConfig['roles'];
  providers: SessionConfig['providers'];
  loading: boolean;
  /**
   * True once a session-scoped read has succeeded — `roles.primary.resolved`
   * reflects the session's actual selection. False while only the sessionless
   * fallback has loaded (a brand-new conversation with no DB row yet, or no
   * session at all). Since FRE-938 the sessionless read also resolves a
   * catalog default per role, so `resolved` is populated either way —
   * consumers decide per their own UI whether `false` should still suppress
   * it (`ModelPicker` shows "not yet set"; `ObserveView` shows the default,
   * labelled via its own "showing catalog defaults" banner).
   */
  hydrated: boolean;
  refetch: () => void;
}

/**
 * Fetch the model-picker + observe-view config for `sessionId`.
 *
 * Falls back to the sessionless `GET /api/v1/config` when the session has no
 * DB row yet (404 — a brand-new conversation before the first message),
 * closing the gap a picker would otherwise have before the first send
 * (ADR-0121 T5 / FRE-920, codex plan-review finding #1).
 */
export function useSessionConfig(sessionId: string | undefined): UseSessionConfigResult {
  const [roles, setRoles] = useState<SessionConfig['roles']>({});
  const [providers, setProviders] = useState<SessionConfig['providers']>([]);
  const [loading, setLoading] = useState(true);
  const [hydrated, setHydrated] = useState(false);
  const generationRef = useRef(0);

  const load = useCallback(() => {
    const generation = ++generationRef.current;
    const applyIfCurrent = (config: SessionConfig, isHydrated: boolean) => {
      if (generationRef.current !== generation) return;
      setRoles(config.roles);
      setProviders(config.providers);
      setHydrated(isHydrated);
      setLoading(false);
    };

    const fetchSessionless = () => {
      void getConfig()
        .then((config) => applyIfCurrent(config, false))
        .catch(() => {
          if (generationRef.current === generation) setLoading(false);
        });
    };

    if (!sessionId) {
      fetchSessionless();
      return;
    }

    void getSessionConfig(sessionId)
      .then((config) => {
        if (config === null) {
          // No DB row yet — brand-new conversation.
          fetchSessionless();
          return;
        }
        applyIfCurrent(config, true);
      })
      .catch(() => {
        if (generationRef.current === generation) setLoading(false);
      });
  }, [sessionId]);

  useEffect(() => {
    setLoading(true);
    setHydrated(false);
    load();
    const interval = setInterval(load, POLL_INTERVAL_MS);
    return () => clearInterval(interval);
  }, [load]);

  return { roles, providers, loading, hydrated, refetch: load };
}
