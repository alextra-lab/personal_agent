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
    document: {
      documentElement: {
        classList: {
          toggle: (name: string, force: boolean) => {
            if (force) classes.add(name);
            else classes.delete(name);
          },
        },
      },
    },
  };

  // The script references bare `localStorage`, `window`, `document` — run it
  // with those bound as locals so it resolves against the fake environment
  // without needing a real DOM.
  const fn = new Function(
    'window',
    'localStorage',
    'document',
    THEME_INIT_SCRIPT,
  );
  fn(fakeWindow, fakeWindow.localStorage, fakeWindow.document);

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
