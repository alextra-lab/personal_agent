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
 * Round 10: the shipped 100vh fix (globals.css) is static and doesn't
 * shrink for the on-screen keyboard, so with no overflow constraint on
 * html/body, WebKit falls back to panning the whole page to keep the
 * focused input visible — confirmed on-device (a screenshot showed this
 * fixed-position overlay itself get dragged off-screen when the keyboard
 * opened). A codex review found overflow:hidden alone unproven to stop
 * that pan without a real device test, so this toggle applies the
 * candidate fix (overflow:hidden + a visualViewport-driven live height)
 * directly on the deployed page for the owner to test with the keyboard,
 * without needing another deploy first. Fully reversible; reverts
 * automatically when the overlay closes. Remove entirely once AC-2 closes.
 */
export function SafeAreaDebugOverlay(): React.JSX.Element | null {
  const [enabled, setEnabled] = useState(false);
  const [keyboardFixOn, setKeyboardFixOn] = useState(false);
  const [data, setData] = useState<SafeAreaMeasurements | null>(null);

  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    if (params.get('debug') === 'safearea') setEnabled(true);

    const toggle = () =>
      setEnabled((prev) => {
        const next = !prev;
        if (!next) setKeyboardFixOn(false);
        return next;
      });
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

  // Candidate keyboard-pan fix, live and reversible. overflow:hidden blocks
  // the browser's own pan-to-reveal-focused-input fallback; the
  // visualViewport-driven height lets the shell shrink for the keyboard
  // instead, once panning is no longer its only option. Deliberately does
  // NOT set the height on mount — innerHeight/visualViewport.height are
  // known to report a stale, undersized value at initial standalone launch
  // (this same ticket) — only trusted once a real resize event fires.
  useEffect(() => {
    const html = document.documentElement;
    const body = document.body;
    if (!(enabled && keyboardFixOn)) {
      html.style.removeProperty('overflow');
      body.style.removeProperty('overflow');
      html.style.removeProperty('height');
      body.style.removeProperty('height');
      return;
    }
    html.style.overflow = 'hidden';
    body.style.overflow = 'hidden';
    const update = () => {
      const h = window.visualViewport?.height ?? window.innerHeight;
      html.style.height = `${h}px`;
      body.style.height = `${h}px`;
      setData(measure());
    };
    window.visualViewport?.addEventListener('resize', update);
    window.addEventListener('resize', update);
    return () => {
      window.visualViewport?.removeEventListener('resize', update);
      window.removeEventListener('resize', update);
      html.style.removeProperty('overflow');
      body.style.removeProperty('overflow');
      html.style.removeProperty('height');
      body.style.removeProperty('height');
    };
  }, [enabled, keyboardFixOn]);

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
      {keyboardFixOn && (
        <div
          style={{
            color: '#ff0',
            fontWeight: 'bold',
            marginTop: '4px',
            textShadow: '0 0 3px #000, 0 0 3px #000, 0 1px 2px #000',
          }}
        >
          KEYBOARD-PAN FIX ACTIVE — not the shipped behavior
        </div>
      )}
      <button
        type="button"
        data-testid="safe-area-keyboard-fix-toggle"
        onClick={() => setKeyboardFixOn((prev) => !prev)}
        style={{
          pointerEvents: 'auto',
          background: keyboardFixOn ? '#ff0' : '#333',
          color: keyboardFixOn ? '#000' : '#0f0',
          border: '1px solid #0f0',
          borderRadius: '4px',
          padding: '4px 8px',
          marginTop: '6px',
          fontFamily: 'monospace',
          fontSize: '11px',
        }}
      >
        {keyboardFixOn ? 'Revert keyboard-pan fix' : 'Try keyboard-pan fix (live, reversible)'}
      </button>
    </div>
  );
}
