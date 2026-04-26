'use client';

import { useEffect } from 'react';
import { useRouter } from 'next/navigation';

import { generateUUID } from '@/lib/uuid';

const LAST_SESSION_KEY = 'seshat_last_session_id';

function isValidUUID(s: string): boolean {
  return /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(s);
}

/**
 * Root page — redirects to the last-known session or mints a new one.
 *
 * Session state lives at /c/{sessionId} so every conversation has a
 * permanent, shareable URL. The last session ID is persisted in
 * localStorage so returning visitors resume where they left off.
 */
export default function Home() {
  const router = useRouter();

  useEffect(() => {
    const stored = localStorage.getItem(LAST_SESSION_KEY);
    const sessionId = stored && isValidUUID(stored) ? stored : generateUUID();
    router.replace(`/c/${sessionId}`);
  }, [router]);

  // Minimal loading state while redirecting (usually sub-frame).
  return (
    <main className="h-full flex flex-col items-center justify-center bg-slate-900 text-slate-500">
      <p className="text-sm">Loading…</p>
    </main>
  );
}
