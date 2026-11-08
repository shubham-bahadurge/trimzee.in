"use strict";

const CACHE_NAME = "trimzee-pwa-v3";
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

self.addEventListener("push", (event) => {
    let data = {};
    if (event.data) {
        try {
            data = event.data.json();
        } catch (error) {
            data = { body: event.data.text() };
        }
    }
    event.waitUntil(self.registration.showNotification(data.title || "Trimzee", {
        body: data.body || "You have a new update.",
        icon: data.icon || "/img/pwa-icon-192.png",
        badge: "/img/pwa-icon-192.png",
        tag: data.tag || "trimzee-update",
        data: { url: data.url || "/" }
    }));
});

self.addEventListener("notificationclick", (event) => {
    event.notification.close();
    const target = new URL(event.notification.data.url || "/", self.location.origin);
    if (target.origin !== self.location.origin) {
        return;
    }
    event.waitUntil(clients.matchAll({ type: "window", includeUncontrolled: true }).then((windows) => {
        const client = windows.find((window) => new URL(window.url).origin === self.location.origin);
        if (client) {
            return client.navigate(target.href).then(() => client.focus());
        }
        return clients.openWindow(target.href);
    }));
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
