import { Suspense } from 'react';

import { SettingsView } from '@/components/SettingsView';

export const metadata = { title: 'Settings — Seshat' };

export default function SettingsPage() {
  return (
    <main className="h-full overflow-y-auto">
      <Suspense fallback={null}>
        <SettingsView view="menu" />
      </Suspense>
    </main>
  );
}
