"use strict";

const CACHE_NAME = "trimzee-pwa-v2";
const APP_SHELL = [
    "/offline.html",
    "/manifest.webmanifest",
    "/img/pwa-icon-192.png",
    "/img/pwa-icon-512.png",
    "/css/site.css",
    "/js/pwa.js"
];
const CACHEABLE_ASSETS = new Set([
    "/manifest.webmanifest",
    "/img/pwa-icon-192.png",
    "/img/pwa-icon-512.png",
    "/css/site.css",
    "/js/pwa.js"
]);

self.addEventListener("install", (event) => {
    event.waitUntil(caches.open(CACHE_NAME).then((cache) => cache.addAll(APP_SHELL)));
    self.skipWaiting();
});

self.addEventListener("activate", (event) => {
    event.waitUntil(
        caches.keys().then((keys) => Promise.all(
            keys.filter((key) => key.startsWith("trimzee-pwa-") && key !== CACHE_NAME)
                .map((key) => caches.delete(key))
        )).then(() => self.clients.claim())
    );
});

self.addEventListener("fetch", (event) => {
    const request = event.request;
    if (request.method !== "GET" || new URL(request.url).origin !== self.location.origin) {
        return;
    }

    if (request.mode === "navigate") {
        event.respondWith(fetch(request).catch(() => caches.match("/offline.html")));
        return;
    }

    const path = new URL(request.url).pathname;
    if (CACHEABLE_ASSETS.has(path)) {
        event.respondWith(
            fetch(request).then((response) => {
                if (!response.ok) {
                    return response;
                }
                return caches.open(CACHE_NAME).then((cache) =>
                    cache.put(request, response.clone()).then(() => response)
                );
            }).catch(() => caches.match(request))
        );
    }
});
