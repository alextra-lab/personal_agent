import { StreamingChat } from '@/components/StreamingChat';

interface Props {
  params: Promise<{ sessionId: string }>;
}

export default async function SessionPage({ params }: Props) {
  const { sessionId } = await params;
  return (
    <main className="h-full flex flex-col">
      <StreamingChat sessionId={sessionId} />
    </main>
  );
}
