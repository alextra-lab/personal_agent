import { ArtifactsIndex } from '@/components/ArtifactsIndex';

export const metadata = { title: 'Artifacts — Seshat' };

export default function ArtifactsPage() {
  return (
    <main className="h-full overflow-y-auto">
      <ArtifactsIndex />
    </main>
  );
}
