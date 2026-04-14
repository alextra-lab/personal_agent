import { StreamingChat } from '@/components/StreamingChat';

/**
 * Root page — renders the full-height streaming chat interface.
 *
 * The session ID is generated client-side on first mount (inside
 * StreamingChat via crypto.randomUUID).  Future: accept a session_id
 * query param to resume an existing conversation.
 */
export default function Home() {
  return (
    <main className="h-full flex flex-col">
      <StreamingChat />
    </main>
  );
}
