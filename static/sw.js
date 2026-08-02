self.addEventListener('install', () => {
  self.skipWaiting();
});

self.addEventListener('activate', e => {
  e.waitUntil(
    (async () => {
      // Clear any caches left by old versions
      const keys = await caches.keys();
      await Promise.all(keys.map(k => caches.delete(k)));
      // Unregister this worker entirely
      await self.registration.unregister();
      // Force all open tabs to reload with no service worker controlling them
      const clientsList = await self.clients.matchAll({ type: 'window' });
      clientsList.forEach(client => client.navigate(client.url));
    })()
  );
});
