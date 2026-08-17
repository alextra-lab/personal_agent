/**
 * Shared trigger contract between SafeAreaDebugOverlay and its gesture
 * trigger (FRE-1269 follow-up). A standalone home-screen PWA has no URL bar,
 * so ?debug=safearea — the overlay's only trigger — was unreachable in
 * exactly the launch mode the diagnostic exists to inspect. This event lets
 * a UI gesture (elsewhere in the tree) toggle the overlay without prop
 * drilling, while the query-param path keeps working unchanged.
 */
export const SAFE_AREA_DEBUG_TOGGLE_EVENT = 'seshat:toggle-safe-area-debug';

export function toggleSafeAreaDebugOverlay(): void {
  window.dispatchEvent(new Event(SAFE_AREA_DEBUG_TOGGLE_EVENT));
}
