'use client';

import { useEffect, useState } from 'react';

import { SAFE_AREA_DEBUG_TOGGLE_EVENT } from '@/lib/safeAreaDebug';

interface SafeAreaMeasurements {
  screenHeight: number;
  innerHeight: number;
  clientHeight: number;
  visualViewportHeight: number | null;
  htmlHeight: number;
  bodyHeight: number;
  safeTopVar: string;
  safeBottomVar: string;
  composerBottom: number | null;
  footerBottom: number | null;
  vh100: number;
  svh100: number;
  dvh100: number;
  displayModeStandalone: boolean;
  scrollY: number;
  htmlScrollHeight: number;
  bodyScrollHeight: number;
  visualViewportOffsetTop: number | null;
  visualViewportPageTop: number | null;
  activeElementTag: string | null;
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
    safeTopVar: getComputedStyle(html).getPropertyValue('--safe-top').trim(),
    safeBottomVar: getComputedStyle(html).getPropertyValue('--safe-bottom').trim(),
    composerBottom: composer ? composer.getBoundingClientRect().bottom : null,
    footerBottom: footer ? footer.getBoundingClientRect().bottom : null,
    vh100,
    svh100,
    dvh100,
    displayModeStandalone: window.matchMedia('(display-mode: standalone)').matches,
    scrollY: window.scrollY,
    htmlScrollHeight: html.scrollHeight,
    bodyScrollHeight: body.scrollHeight,
    visualViewportOffsetTop: window.visualViewport?.offsetTop ?? null,
    visualViewportPageTop: window.visualViewport?.pageTop ?? null,
    activeElementTag: document.activeElement?.tagName ?? null,
  };
}

/**
 * Temporary on-device diagnostic for FRE-1269 — surfaces the exact viewport
 * measurements a real iOS standalone launch can't be read from a Mac-less
 * phone otherwise (no headless harness can emulate this; see the ticket).
 *
 * Two independent triggers, both required: a standalone home-screen PWA has
 * no URL bar, so ?debug=safearea alone is unreachable in exactly the launch
 * mode this diagnostic exists to inspect. The header's 5-rapid-tap gesture
 * (StreamingChat.tsx) works there too, via SAFE_AREA_DEBUG_TOGGLE_EVENT.
 *
 * The real standalone-scoped fix has since shipped (globals.css). A
 * round-7/8 live "100vh experiment" toggle that lived here — used to
 * validate the fix on-device before committing to it — is retired now that
 * the fix is real: reverting it in standalone mode would no longer restore
 * old (broken) behavior, since removing the inline override just reveals
 * the same shipped CSS rule underneath. Kept as read-only measurements for
 * this deploy in case the owner's final validation surfaces something an
 * orientation change, keyboard, or background/resume cycle exposes that
 * the single-screenshot experiment didn't cover. Remove entirely once AC-2
 * closes.
 */
export function SafeAreaDebugOverlay(): React.JSX.Element | null {
  const [enabled, setEnabled] = useState(false);
  const [data, setData] = useState<SafeAreaMeasurements | null>(null);

  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    if (params.get('debug') === 'safearea') setEnabled(true);

    const toggle = () => setEnabled((prev) => !prev);
    window.addEventListener(SAFE_AREA_DEBUG_TOGGLE_EVENT, toggle);
    return () => window.removeEventListener(SAFE_AREA_DEBUG_TOGGLE_EVENT, toggle);
  }, []);

  useEffect(() => {
    if (!enabled) {
      setData(null);
      return;
    }
    const update = () => setData(measure());
    update();
    window.addEventListener('resize', update);
    window.visualViewport?.addEventListener('resize', update);
    return () => {
      window.removeEventListener('resize', update);
      window.visualViewport?.removeEventListener('resize', update);
    };
  }, [enabled]);

  if (!enabled || !data) return null;

  const rows: Array<[string, string]> = [
    ['screen.height', String(data.screenHeight)],
    ['innerHeight', String(data.innerHeight)],
    ['html.clientHeight', String(data.clientHeight)],
    ['visualViewport.height', String(data.visualViewportHeight)],
    ['html rect height', String(data.htmlHeight)],
    ['body rect height', String(data.bodyHeight)],
    ['--safe-top', data.safeTopVar],
    ['--safe-bottom', data.safeBottomVar],
    ['composer bottom', String(data.composerBottom)],
    ['footer bottom', String(data.footerBottom)],
    ['100vh probe', String(data.vh100)],
    ['100svh probe', String(data.svh100)],
    ['100dvh probe', String(data.dvh100)],
    ['display-mode: standalone', String(data.displayModeStandalone)],
    ['scrollY', String(data.scrollY)],
    ['html.scrollHeight', String(data.htmlScrollHeight)],
    ['body.scrollHeight', String(data.bodyScrollHeight)],
    ['visualViewport.offsetTop', String(data.visualViewportOffsetTop)],
    ['visualViewport.pageTop', String(data.visualViewportPageTop)],
    ['activeElement', String(data.activeElementTag)],
  ];

  return (
    <div
      data-testid="safe-area-debug-overlay"
      style={{
        position: 'fixed',
        // Starts below the header's own control zone rather than top:0 —
        // matches the header's own safe-area-aware offset so it clears the
        // taller real-device header too, not just the zero-inset
        // headless/CI case.
        top: 'calc(env(safe-area-inset-top, 0px) + 4rem)',
        left: 0,
        right: 0,
        zIndex: 9999,
        // Translucent, not opaque — a round-8 screenshot meant to check the
        // header's position showed no header at all, because an earlier,
        // near-opaque background was painting over it. Kept translucent
        // with a text-shadow for legibility, so a screenshot can show both
        // the numbers and whatever real UI is behind them.
        background: 'rgba(0,0,0,0.35)',
        color: '#0f0',
        textShadow: '0 0 3px #000, 0 0 3px #000, 0 1px 2px #000',
        fontFamily: 'monospace',
        fontSize: '11px',
        lineHeight: 1.4,
        padding: '8px',
        // Read-only — nothing in here is interactive, so it never blocks
        // taps on whatever's underneath (e.g. the header's own close
        // gesture).
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
