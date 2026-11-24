import { SharedLinksView } from '@/components/SharedLinksView';

export const metadata = { title: 'Shared links — Seshat' };

export default function SharedLinksPage() {
  return (
    <main className="h-full overflow-y-auto">
      <SharedLinksView />
    </main>
  );
}
