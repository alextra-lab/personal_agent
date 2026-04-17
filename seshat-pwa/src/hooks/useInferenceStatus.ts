'use client';

import { useState, useEffect, useRef } from 'react';

import { SESHAT_API } from '@/lib/agui-client';

export type InferenceStatus = 'unknown' | 'up' | 'down';

export interface InferenceStatusResult {
  status: InferenceStatus;
  latencyMs: number | null;
}

/**
 * Poll GET /api/inference/status every 60 seconds while the local profile is active.
 *
 * Returns "unknown" until the first check completes, "up"/"down" thereafter.
 * Polling stops immediately when `active` becomes false.
 */
export function useInferenceStatus(active: boolean): InferenceStatusResult {
  const [result, setResult] = useState<InferenceStatusResult>({
    status: 'unknown',
    latencyMs: null,
  });
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null);

  useEffect(() => {
    if (!active) {
      if (intervalRef.current !== null) {
        clearInterval(intervalRef.current);
        intervalRef.current = null;
      }
      setResult({ status: 'unknown', latencyMs: null });
      return;
    }

    const check = async () => {
      try {
        const resp = await fetch(`${SESHAT_API}/api/inference/status`);
        const data = (await resp.json()) as {
          local: 'up' | 'down';
          latency_ms: number | null;
        };
        setResult({ status: data.local, latencyMs: data.latency_ms });
      } catch {
        setResult({ status: 'down', latencyMs: null });
      }
    };

    check(); // immediate check on activation
    intervalRef.current = setInterval(check, 60_000);

    return () => {
      if (intervalRef.current !== null) {
        clearInterval(intervalRef.current);
        intervalRef.current = null;
      }
    };
  }, [active]);

  return result;
}
