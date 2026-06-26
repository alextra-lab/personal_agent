'use client';

import { useEffect } from 'react';

/**
 * Registers /sw.js and auto-reloads when a new SW takes control.
 *
 * Mounted once in RootLayout. Guards:
 *   - SSR: useEffect only runs in the browser
 *   - No SW support: early return
 *   - First install: captures wasControlled before register to skip reload
 *     when null→SW (avoids an immediate reload on first-ever SW install)
 */
export function RegisterSW(): null {
  useEffect(() => {
    const sw = navigator.serviceWorker;
    if (!sw) return;

    const wasControlled = Boolean(sw.controller);
    let reloading = false;

    function handleControllerChange(): void {
      if (reloading || !wasControlled) return;
      reloading = true;
      window.location.reload();
    }

    sw.register('/sw.js').catch((err: unknown) => {
      if (process.env.NODE_ENV !== 'production') {
        console.error('[SW] registration failed', err);
      }
    });

    sw.addEventListener('controllerchange', handleControllerChange);

    return () => {
      sw.removeEventListener('controllerchange', handleControllerChange);
    };
  }, []);

  return null;
}
