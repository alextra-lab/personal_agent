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
    // Round-7 additions (codex adversarial review): a 100vh probe reading
    // the physical screen height proves CSS unit *resolution*, not that
    // content is actually *paintable* there (WebKit 301994's own report
    // draws exactly this distinction) — scroll state and visualViewport
    // offsets are what can actually tell the two apart.
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
 * mode this diagnostic exists to inspect (FRE-1269 follow-up). The header's
 * 5-rapid-tap gesture (StreamingChat.tsx) works there too, via
 * SAFE_AREA_DEBUG_TOGGLE_EVENT.
 *
 * Round-7 addition: a live "100vh experiment" toggle applies the candidate
 * fix (html/body height:100vh, via inline style — no rebuild needed) on the
 * already-deployed page so the owner can screenshot the *actual rendered
 * result*, not just a hidden probe's CSS resolution — codex's adversarial
 * review found the probe alone can't distinguish a value WebKit resolves
 * from one it actually paints (WebKit bug 301994's own framing). Reverts
 * automatically when the overlay closes, so it can never persist unnoticed.
 *
 * Remove all of this once the standalone bottom-gap mechanism is confirmed
 * and fixed.
 */
export function SafeAreaDebugOverlay(): React.JSX.Element | null {
  const [enabled, setEnabled] = useState(false);
  const [experimentOn, setExperimentOn] = useState(false);
  const [data, setData] = useState<SafeAreaMeasurements | null>(null);

  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    if (params.get('debug') === 'safearea') setEnabled(true);

    const toggle = () =>
      setEnabled((prev) => {
        const next = !prev;
        if (!next) setExperimentOn(false);
        return next;
      });
    window.addEventListener(SAFE_AREA_DEBUG_TOGGLE_EVENT, toggle);
    return () => window.removeEventListener(SAFE_AREA_DEBUG_TOGGLE_EVENT, toggle);
  }, []);

  // Applies/reverts the candidate fix via inline style (higher specificity
  // than the .h-full Tailwind class — no rebuild needed to test it), THEN
  // re-measures in the same effect so ordering is guaranteed — style change
  // lands before the read. Previously the measurement effect only depended
  // on [enabled], so toggling the experiment changed the DOM but never
  // refreshed the displayed numbers, showing stale pre-toggle figures (a
  // real bug: the owner's round-7 screenshot never demonstrated the
  // experiment's effect at all because of this). The cleanup runs on every
  // dependency change AND unmount, so closing the overlay (enabled -> false)
  // or the component ever unmounting always reverts it — an experiment can
  // never be left silently applied.
  useEffect(() => {
    const html = document.documentElement;
    const body = document.body;
    if (enabled && experimentOn) {
      html.style.height = '100vh';
      body.style.height = '100vh';
    } else {
      html.style.removeProperty('height');
      body.style.removeProperty('height');
    }
    setData(enabled ? measure() : null);
    return () => {
      html.style.removeProperty('height');
      body.style.removeProperty('height');
    };
  }, [enabled, experimentOn]);

  // Keeps the numbers live across real environmental changes (keyboard,
  // rotation) while the overlay stays open with the experiment state
  // unchanged — independent of the toggle above.
  useEffect(() => {
    if (!enabled) return;
    const update = () => setData(measure());
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
        // Starts below the header's own control zone (hamburger left, New
        // button right, title center) rather than at top:0 — self-review
        // found the experiment button's pointerEvents:'auto' hit-box
        // otherwise overlaps and steals taps from the hamburger button.
        // Matches the header's own safe-area-aware offset (StreamingChat.tsx)
        // so it clears the taller real-device header too, not just the
        // zero-inset headless/CI case.
        top: 'calc(env(safe-area-inset-top, 0px) + 4rem)',
        left: 0,
        right: 0,
        zIndex: 9999,
        // Near-opaque background used to paint over whatever's underneath —
        // including the header this round is specifically trying to
        // inspect, and (depending on content length) the composer at the
        // bottom of the screen too. A round-8 screenshot meant to check the
        // header's position showed no header at all, because this overlay
        // was covering it. Kept deliberately translucent instead, with a
        // strong text-shadow for legibility, so a single screenshot can show
        // both the numbers and whatever real UI is behind them.
        background: 'rgba(0,0,0,0.35)',
        color: '#0f0',
        textShadow: '0 0 3px #000, 0 0 3px #000, 0 1px 2px #000',
        fontFamily: 'monospace',
        fontSize: '11px',
        lineHeight: 1.4,
        padding: '8px',
        // Lets taps reach whatever is underneath (e.g. the header's own
        // close gesture) everywhere except the experiment button below,
        // which explicitly re-enables pointer events on itself.
        pointerEvents: 'none',
      }}
    >
      {experimentOn && (
        <div
          style={{
            color: '#ff0',
            fontWeight: 'bold',
            marginBottom: '4px',
            textShadow: '0 0 3px #000, 0 0 3px #000, 0 1px 2px #000',
          }}
        >
          100vh EXPERIMENT ACTIVE — not the shipped behavior
        </div>
      )}
      <button
        type="button"
        data-testid="safe-area-experiment-toggle"
        onClick={() => setExperimentOn((prev) => !prev)}
        style={{
          pointerEvents: 'auto',
          background: experimentOn ? '#ff0' : '#333',
          color: experimentOn ? '#000' : '#0f0',
          border: '1px solid #0f0',
          borderRadius: '4px',
          padding: '4px 8px',
          marginBottom: '6px',
          fontFamily: 'monospace',
          fontSize: '11px',
        }}
      >
        {experimentOn ? 'Revert 100vh experiment' : 'Try 100vh experiment (live, reversible)'}
      </button>
      {rows.map(([label, value]) => (
        <div key={label}>
          {label}: {value}
        </div>
      ))}
    </div>
  );
}
