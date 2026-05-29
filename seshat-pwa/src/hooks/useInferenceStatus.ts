'use client';

import { useState, useEffect, useRef } from 'react';

import { SESHAT_API } from '@/lib/agui-client';
import type { ExecutionProfile } from '@/lib/types';

export type InferenceStatus = 'unknown' | 'up' | 'down';

export interface InferenceStatusResult {
  status: InferenceStatus;
  latencyMs: number | null;
}

/**
 * Poll GET /api/inference/status?profile=<profile> every 60 seconds for the
 * given execution profile's inference path (FRE-421).
 *
 * - `local` live-probes the Mac SLM tunnel.
 * - `cloud` reports whether the cloud provider is configured.
 *
 * Pass `null` to stop polling. Returns "unknown" until the first check
 * completes, "up"/"down" thereafter.
 */
export function useInferenceStatus(profile: ExecutionProfile | null): InferenceStatusResult {
  const [result, setResult] = useState<InferenceStatusResult>({
    status: 'unknown',
    latencyMs: null,
  });
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null);

  useEffect(() => {
    if (!profile) {
      if (intervalRef.current !== null) {
        clearInterval(intervalRef.current);
        intervalRef.current = null;
      }
      setResult({ status: 'unknown', latencyMs: null });
      return;
    }

    const check = async () => {
      try {
        const resp = await fetch(`${SESHAT_API}/api/inference/status?profile=${profile}`);
        const data = (await resp.json()) as {
          status: 'up' | 'down' | 'unknown';
          latency_ms: number | null;
        };
        setResult({ status: data.status, latencyMs: data.latency_ms });
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
  }, [profile]);

  return result;
}
