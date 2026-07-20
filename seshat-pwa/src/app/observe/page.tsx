import { Suspense } from 'react';

import { ObserveView } from '@/components/ObserveView';

export const metadata = { title: 'Observe — Seshat' };

export default function ObservePage() {
  return (
    <main className="h-full overflow-y-auto">
      <Suspense fallback={null}>
        <ObserveView />
      </Suspense>
    </main>
  );
}
