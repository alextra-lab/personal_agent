/**
 * Seshat PWA Service Worker
 *
 * Strategy:
 *   - Precache the app shell ('/', manifest) for offline boot.
 *   - Network-first for HTML and Next.js build outputs (so deploys reach the
 *     installed PWA without manual cache eviction). Falls back to cache offline.
 *   - Network-only for the API (/stream, /chat, /api/*).
 *
 * Bump CACHE_NAME on every PWA deploy that changes the shell so the
 * activate handler evicts the previous version.
 */

const CACHE_NAME = 'seshat-v22-artifact-export';

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

// Fetch strategy:
//   - /stream, /chat, /api/* → bypass the SW entirely (network-only)
//   - everything else → network-first, fall back to cache on failure.
//     Successful network responses are stored in the cache so the PWA still
//     boots offline. This avoids the iOS-PWA bug where a cache-first shell
//     keeps serving the old bundle forever after a deploy.
self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);

  if (
    url.pathname.startsWith('/stream') ||
    url.pathname.startsWith('/chat') ||
    url.pathname.startsWith('/api/')
  ) {
    return;
  }

  if (event.request.method !== 'GET') {
    return;
  }

  event.respondWith(
    (async () => {
      try {
        const response = await fetch(event.request);
        if (response && response.ok && response.type === 'basic') {
          const cache = await caches.open(CACHE_NAME);
          cache.put(event.request, response.clone());
        }
        return response;
      } catch (err) {
        const cached = await caches.match(event.request);
        if (cached) return cached;
        throw err;
      }
    })(),
  );
});
