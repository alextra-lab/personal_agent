'use client';

import { useEffect } from 'react';
import { useRouter } from 'next/navigation';

import { generateUUID } from '@/lib/uuid';
import { resolveLastSessionId } from '@/lib/session';

/**
 * Root page — redirects to the most recent session or mints a new one.
 *
 * Session state lives at /c/{sessionId} so every conversation has a
 * permanent, shareable URL. A returning visitor resumes their most recent
 * session — from localStorage when present (no network call), or from the
 * server's session list when it is missing, so a visitor whose storage was
 * cleared still resumes their history instead of landing in an unrelated
 * new conversation (FRE-938). Only a user with no sessions at all gets a
 * new one.
 */
export default function Home() {
  const router = useRouter();

  useEffect(() => {
    let cancelled = false;
    resolveLastSessionId().then((sessionId) => {
      if (cancelled) return;
      router.replace(`/c/${sessionId ?? generateUUID()}`);
    });
    return () => {
      cancelled = true;
    };
  }, [router]);

  // Minimal loading state while redirecting (usually sub-frame).
  return (
    <main className="h-full flex flex-col items-center justify-center bg-slate-900 text-slate-500">
      <p className="text-sm">Loading…</p>
    </main>
  );
}
