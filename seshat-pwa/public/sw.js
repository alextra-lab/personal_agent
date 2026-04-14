/**
 * Seshat PWA Service Worker
 *
 * Phase 1: cache-first for static assets; network-only for API calls.
 *
 * Phase 2 (planned): background sync for offline message queuing.
 */

const CACHE_NAME = 'seshat-v1';

const PRECACHE_URLS = [
  '/',
  '/manifest.json',
];

// Install — pre-cache shell
self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(PRECACHE_URLS)),
  );
  self.skipWaiting();
});

// Activate — clear old caches
self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(
        keys
          .filter((key) => key !== CACHE_NAME)
          .map((key) => caches.delete(key)),
      ),
    ),
  );
  self.clients.claim();
});

// Fetch — cache-first for static; network-only for /stream, /chat
self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);

  if (url.pathname.startsWith('/stream') || url.pathname.startsWith('/chat')) {
    return;
  }

  event.respondWith(
    caches.match(event.request).then(
      (cached) => cached ?? fetch(event.request),
    ),
  );
});
