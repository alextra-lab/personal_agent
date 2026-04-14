/**
 * Seshat PWA Service Worker
 *
 * Phase 1: cache-first for static assets; network-only for API calls.
 *
 * Phase 2 (planned): background sync for offline message queuing.
 *
 * Registration: next.config.js will be extended with next-pwa to auto-
 * register this file. For now it is a manual stub.
 */

/// <reference lib="webworker" />
declare const self: ServiceWorkerGlobalScope;

const CACHE_NAME = 'seshat-v1';

// Assets to pre-cache on install.
const PRECACHE_URLS: string[] = [
  '/',
  '/manifest.json',
];

// --------------------------------------------------------------------------
// Install — pre-cache shell
// --------------------------------------------------------------------------

self.addEventListener('install', (event: ExtendableEvent) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(PRECACHE_URLS)),
  );
  // Activate immediately without waiting for existing tabs to close.
  self.skipWaiting();
});

// --------------------------------------------------------------------------
// Activate — clear old caches
// --------------------------------------------------------------------------

self.addEventListener('activate', (event: ExtendableEvent) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(
        keys
          .filter((key) => key !== CACHE_NAME)
          .map((key) => caches.delete(key)),
      ),
    ),
  );
  // Take control of all tabs immediately.
  self.clients.claim();
});

// --------------------------------------------------------------------------
// Fetch — cache-first for static; network-only for /stream, /chat
// --------------------------------------------------------------------------

self.addEventListener('fetch', (event: FetchEvent) => {
  const { request } = event;
  const url = new URL(request.url);

  // Never cache SSE streams or chat API calls.
  if (url.pathname.startsWith('/stream') || url.pathname.startsWith('/chat')) {
    // Pass through to network.
    return;
  }

  // Cache-first for everything else.
  event.respondWith(
    caches.match(request).then(
      (cached) => cached ?? fetch(request),
    ),
  );
});
