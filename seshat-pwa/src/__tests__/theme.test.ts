/**
 * FRE-1264 AC-1/AC-2 — the inline theme-init script that runs before
 * hydration. Verified by `eval`-ing the actual script string against a
 * mocked browser environment, so a change to `theme.ts` that breaks the
 * runtime logic fails here rather than only in the Playwright e2e suite.
 */

import { describe, it, expect, beforeEach } from 'vitest';

import { THEME_INIT_SCRIPT, THEME_STORAGE_KEY } from '@/lib/theme';

function runInitScript(opts: { stored: string | null; systemPrefersDark: boolean }) {
  const store = new Map<string, string>();
  if (opts.stored !== null) store.set(THEME_STORAGE_KEY, opts.stored);

  const classes = new Set<string>();
  const fakeWindow = {
    localStorage: {
      getItem: (key: string) => store.get(key) ?? null,
    },
    matchMedia: (query: string) => ({
      matches: query.includes('dark') && opts.systemPrefersDark,
    }),
    requestAnimationFrame: (cb: FrameRequestCallback) => {
      cb(0);
      return 0;
    },
    document: {
      documentElement: {
        classList: {
          add: (name: string) => classes.add(name),
          remove: (name: string) => classes.delete(name),
          toggle: (name: string, force: boolean) => {
            if (force) classes.add(name);
            else classes.delete(name);
          },
        },
      },
    },
  };

  // The script references bare `localStorage`, `window`, `document`,
  // `requestAnimationFrame` — run it with those bound as locals so it
  // resolves against the fake environment without needing a real DOM.
  const fn = new Function(
    'window',
    'localStorage',
    'document',
    'requestAnimationFrame',
    THEME_INIT_SCRIPT,
  );
  fn(fakeWindow, fakeWindow.localStorage, fakeWindow.document, fakeWindow.requestAnimationFrame);

  return classes.has('dark');
}

describe('THEME_INIT_SCRIPT', () => {
  it('follows the system preference when no override is stored (dark)', () => {
    expect(runInitScript({ stored: null, systemPrefersDark: true })).toBe(true);
  });

  it('follows the system preference when no override is stored (light)', () => {
    expect(runInitScript({ stored: null, systemPrefersDark: false })).toBe(false);
  });

  it('a stored dark override wins over a light system preference', () => {
    expect(runInitScript({ stored: 'dark', systemPrefersDark: false })).toBe(true);
  });

  it('a stored light override wins over a dark system preference', () => {
    expect(runInitScript({ stored: 'light', systemPrefersDark: true })).toBe(false);
  });

  it('falls back to system preference on an unrecognized stored value', () => {
    expect(runInitScript({ stored: 'sepia', systemPrefersDark: true })).toBe(true);
  });
});

/**
 * FRE-1425 — a first paint that lands before this script runs (a slow JS
 * parse/eval under CI's resource-constrained runners) leaves `transition-colors`
 * elements animating from their light-theme colours to dark's once the class
 * is corrected, and Playwright can sample that animation mid-flight (observed:
 * ModelPicker's model-name text measured 3.45:1-4.13:1, both below the 4.5 AA
 * floor, even though the settled dark colour clears it at 4.62:1). The fix
 * disables all transitions for the two animation frames spanning theme
 * resolution, so the correction — if a mismatched frame ever paints — is
 * instant rather than animated.
 */
describe('THEME_INIT_SCRIPT — transition suppression (FRE-1425)', () => {
  function runInitScriptFull(opts: { stored: string | null; systemPrefersDark: boolean }) {
    const store = new Map<string, string>();
    if (opts.stored !== null) store.set(THEME_STORAGE_KEY, opts.stored);

    const classes = new Set<string>();
    const rafQueue: FrameRequestCallback[] = [];
    const fakeWindow = {
      localStorage: {
        getItem: (key: string) => store.get(key) ?? null,
      },
      matchMedia: (query: string) => ({
        matches: query.includes('dark') && opts.systemPrefersDark,
      }),
      requestAnimationFrame: (cb: FrameRequestCallback) => {
        rafQueue.push(cb);
        return rafQueue.length;
      },
      document: {
        documentElement: {
          classList: {
            add: (name: string) => classes.add(name),
            remove: (name: string) => classes.delete(name),
            toggle: (name: string, force: boolean) => {
              if (force) classes.add(name);
              else classes.delete(name);
            },
          },
        },
      },
    };

    const fn = new Function(
      'window',
      'localStorage',
      'document',
      'requestAnimationFrame',
      THEME_INIT_SCRIPT,
    );
    fn(fakeWindow, fakeWindow.localStorage, fakeWindow.document, fakeWindow.requestAnimationFrame);

    const flushOneFrame = () => {
      const due = rafQueue.splice(0, rafQueue.length);
      due.forEach((cb) => cb(0));
    };

    return { classes, flushOneFrame };
  }

  it('adds the transition-suppressing class synchronously, before any frame is flushed', () => {
    const { classes } = runInitScriptFull({ stored: 'dark', systemPrefersDark: false });
    expect(classes.has('theme-init')).toBe(true);
  });

  it('keeps the transition-suppressing class through the first animation frame', () => {
    const { classes, flushOneFrame } = runInitScriptFull({ stored: 'dark', systemPrefersDark: false });
    flushOneFrame();
    expect(classes.has('theme-init')).toBe(true);
  });

  it('removes the transition-suppressing class after two animation frames', () => {
    const { classes, flushOneFrame } = runInitScriptFull({ stored: 'dark', systemPrefersDark: false });
    flushOneFrame();
    flushOneFrame();
    expect(classes.has('theme-init')).toBe(false);
  });

  it('still resolves the theme correctly alongside the suppression class', () => {
    const { classes } = runInitScriptFull({ stored: null, systemPrefersDark: true });
    expect(classes.has('dark')).toBe(true);
  });
});
