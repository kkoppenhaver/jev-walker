// Installability only. The walk needs the network for every decision, so nothing
// is cached and every request goes straight through.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));
