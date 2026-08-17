'use client';

import { useEffect, useState } from 'react';

interface SafeAreaMeasurements {
  screenHeight: number;
  innerHeight: number;
  clientHeight: number;
  visualViewportHeight: number | null;
  htmlHeight: number;
  bodyHeight: number;
  safeBottomVar: string;
  composerBottom: number | null;
  footerBottom: number | null;
  vh100: number;
  svh100: number;
  dvh100: number;
  displayModeStandalone: boolean;
}

function measure(): SafeAreaMeasurements {
  const html = document.documentElement;
  const body = document.body;
  const composer = document.querySelector('[data-testid="composer-container"]');
  const footer = document.querySelector('footer');

  // Probe elements: a unit's real pixel value can only be read back via a
  // rendered box, not computed from the unit name itself.
  const probe = document.createElement('div');
  probe.style.cssText = 'position:absolute;visibility:hidden;pointer-events:none;width:0;';
  const makeUnitProbe = (unit: string) => {
    const el = document.createElement('div');
    el.style.height = `100${unit}`;
    return el;
  };
  const vhEl = makeUnitProbe('vh');
  const svhEl = makeUnitProbe('svh');
  const dvhEl = makeUnitProbe('dvh');
  probe.append(vhEl, svhEl, dvhEl);
  document.body.appendChild(probe);
  const vh100 = vhEl.getBoundingClientRect().height;
  const svh100 = svhEl.getBoundingClientRect().height;
  const dvh100 = dvhEl.getBoundingClientRect().height;
  document.body.removeChild(probe);

  return {
    screenHeight: window.screen.height,
    innerHeight: window.innerHeight,
    clientHeight: html.clientHeight,
    visualViewportHeight: window.visualViewport?.height ?? null,
    htmlHeight: html.getBoundingClientRect().height,
    bodyHeight: body.getBoundingClientRect().height,
    safeBottomVar: getComputedStyle(html).getPropertyValue('--safe-bottom').trim(),
    composerBottom: composer ? composer.getBoundingClientRect().bottom : null,
    footerBottom: footer ? footer.getBoundingClientRect().bottom : null,
    vh100,
    svh100,
    dvh100,
    displayModeStandalone: window.matchMedia('(display-mode: standalone)').matches,
  };
}

/**
 * Temporary on-device diagnostic for FRE-1269 — surfaces the exact viewport
 * measurements a real iOS standalone launch can't be read from a Mac-less
 * phone otherwise (no headless harness can emulate this; see the ticket).
 * Invisible unless the URL carries ?debug=safearea. Remove once the
 * standalone bottom-gap mechanism is confirmed and fixed.
 */
export function SafeAreaDebugOverlay(): React.JSX.Element | null {
  const [enabled, setEnabled] = useState(false);
  const [data, setData] = useState<SafeAreaMeasurements | null>(null);

  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    if (params.get('debug') !== 'safearea') return;
    setEnabled(true);

    const update = () => setData(measure());
    update();
    window.addEventListener('resize', update);
    window.visualViewport?.addEventListener('resize', update);
    return () => {
      window.removeEventListener('resize', update);
      window.visualViewport?.removeEventListener('resize', update);
    };
  }, []);

  if (!enabled || !data) return null;

  const rows: Array<[string, string]> = [
    ['screen.height', String(data.screenHeight)],
    ['innerHeight', String(data.innerHeight)],
    ['html.clientHeight', String(data.clientHeight)],
    ['visualViewport.height', String(data.visualViewportHeight)],
    ['html rect height', String(data.htmlHeight)],
    ['body rect height', String(data.bodyHeight)],
    ['--safe-bottom', data.safeBottomVar],
    ['composer bottom', String(data.composerBottom)],
    ['footer bottom', String(data.footerBottom)],
    ['100vh probe', String(data.vh100)],
    ['100svh probe', String(data.svh100)],
    ['100dvh probe', String(data.dvh100)],
    ['display-mode: standalone', String(data.displayModeStandalone)],
  ];

  return (
    <div
      data-testid="safe-area-debug-overlay"
      style={{
        position: 'fixed',
        top: 0,
        left: 0,
        right: 0,
        zIndex: 9999,
        background: 'rgba(0,0,0,0.85)',
        color: '#0f0',
        fontFamily: 'monospace',
        fontSize: '11px',
        lineHeight: 1.4,
        padding: '8px',
        pointerEvents: 'none',
      }}
    >
      {rows.map(([label, value]) => (
        <div key={label}>
          {label}: {value}
        </div>
      ))}
    </div>
  );
}
