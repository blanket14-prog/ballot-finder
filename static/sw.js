// Minimal service worker — makes app installable, no offline caching needed
const CACHE = 'ballot-finder-v1';

self.addEventListener('install', e => {
  self.skipWaiting();
});

self.addEventListener('activate', e => {
  self.clients.claim();
});

// Network-first: always fetch fresh data, no caching
self.addEventListener('fetch', e => {
  e.respondWith(fetch(e.request).catch(() => caches.match(e.request)));
});
