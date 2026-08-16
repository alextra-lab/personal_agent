/**
 * Theme resolution (FRE-1264) — light/dark follows the system preference,
 * with a stored override that wins.
 *
 * `THEME_INIT_SCRIPT` runs as a `beforeInteractive` inline script in
 * `layout.tsx` so the `dark` class lands on `<html>` before hydration —
 * resolving the theme in a React effect would flash the wrong theme on
 * every load.
 */

export const THEME_STORAGE_KEY = 'seshat-theme-override';

export const THEME_INIT_SCRIPT = `(function(){try{var s=localStorage.getItem('${THEME_STORAGE_KEY}');var d=s==='dark'||(s!=='light'&&window.matchMedia('(prefers-color-scheme: dark)').matches);document.documentElement.classList.toggle('dark',d);}catch(e){}})();`;
