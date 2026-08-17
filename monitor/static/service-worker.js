"use strict";

const CACHE_NAME = "server-monitor-shell-v3";
const SHELL_FILES = [
  "/",
  "/static/style.css",
  "/static/app_logic.js",
  "/static/app.js",
  "/static/icon.svg",
  "/static/vendor/xterm/xterm.css",
  "/static/vendor/xterm/xterm.js",
  "/static/vendor/xterm/addon-fit.js",
];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE_NAME).then((cache) => cache.addAll(SHELL_FILES)));
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) => Promise.all(keys.filter((key) => key !== CACHE_NAME).map((key) => caches.delete(key)))),
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  const url = new URL(request.url);
  if (request.method !== "GET" || url.origin !== self.location.origin || url.pathname.startsWith("/api/")) return;
  event.respondWith(
    fetch(request).then((response) => {
      if (response.ok && ["document", "script", "style", "image"].includes(request.destination)) {
        const copy = response.clone();
        caches.open(CACHE_NAME).then((cache) => cache.put(request, copy));
      }
      return response;
    }).catch(() => caches.match(request).then((cached) => cached || caches.match("/"))),
  );
});
