/**
 * Theme resolution (FRE-1264) — light/dark follows the system preference,
 * with a stored override that wins.
 *
 * `THEME_INIT_SCRIPT` runs as a `beforeInteractive` inline script in
 * `layout.tsx` so the `dark` class lands on `<html>` before hydration —
 * resolving the theme in a React effect would flash the wrong theme on
 * every load.
 *
 * FRE-1425: a slow JS parse/eval (observed under CI's resource-constrained
 * runners) can let the browser paint one frame before this script runs, so
 * elements using `transition-colors` animate the correction from light to
 * dark instead of snapping to it — measured contrast on that animated frame
 * fails WCAG AA even though the settled colour passes. The `theme-init`
 * class disables all transitions for the two frames spanning theme
 * resolution, so a mismatched first frame corrects instantly.
 */

export const THEME_STORAGE_KEY = 'seshat-theme-override';

export const THEME_INIT_SCRIPT = `(function(){try{var h=document.documentElement;h.classList.add('theme-init');var s=localStorage.getItem('${THEME_STORAGE_KEY}');var d=s==='dark'||(s!=='light'&&window.matchMedia('(prefers-color-scheme: dark)').matches);h.classList.toggle('dark',d);requestAnimationFrame(function(){requestAnimationFrame(function(){h.classList.remove('theme-init');});});}catch(e){}})();`;
