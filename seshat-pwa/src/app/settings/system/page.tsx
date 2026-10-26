import { Suspense } from 'react';

import { SettingsView } from '@/components/SettingsView';

export const metadata = { title: 'System — Seshat' };

export default function SettingsSystemPage() {
  return (
    <main className="h-full overflow-y-auto">
      <Suspense fallback={null}>
        <SettingsView view="system" />
      </Suspense>
    </main>
  );
}
