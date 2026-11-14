"""Installable-app plumbing: the web app manifest and the service worker.

The service worker caches only the app's own static files (stylesheet, scripts, icons) so the shell opens instantly.
It never stores a page or any other response from the server: statements, balances and transactions are fetched fresh
every time, and when the server cannot be reached the phone shows a plain "offline" notice instead of old figures."""

from __future__ import annotations

import json

from fastapi import FastAPI
from fastapi.responses import JSONResponse, Response

THEME = "#2457d6"
MANIFEST = {
    "name": "Kosh",
    "short_name": "Kosh",
    "description": "Your own finance tracker for Indian assets, on your own server.",
    "start_url": "/",
    "scope": "/",
    "display": "standalone",
    "orientation": "any",
    "background_color": "#12151c",
    "theme_color": THEME,
    "icons": [
        {"src": "/static/icons/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
        {"src": "/static/icons/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
        {"src": "/static/icons/icon-maskable-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
    ],
}

OFFLINE_PAGE = ("<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
                "<title>Offline · Kosh</title><body style='font:16px system-ui;margin:12vh 24px;text-align:center'>"
                "<h1>You are offline</h1><p>Kosh needs a connection to your server. Nothing is kept on this device, "
                "so there is nothing to show until you are back online.</p>")

SERVICE_WORKER = """\
// Kosh service worker. Caches static assets only; never pages or data (see finance/web/pwa.py).
const CACHE = 'kosh-static-v1';
const SHELL = ['/static/style.css', '/static/vendor/htmx.min.js', '/static/icons/icon-192.png'];
const OFFLINE = %(offline)s;

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});
self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
    .then(() => self.clients.claim()));
});
self.addEventListener('fetch', e => {
  const req = e.request, url = new URL(req.url);
  if (req.method !== 'GET' || url.origin !== location.origin) return;           // forms, uploads, other sites: untouched
  if (url.pathname.startsWith('/static/')) {                                    // static: network first, cache as fallback
    e.respondWith(fetch(req).then(res => {
      if (res.ok) { const copy = res.clone(); caches.open(CACHE).then(c => c.put(req, copy)); }
      return res;
    }).catch(() => caches.match(req)));
    return;
  }
  if (req.mode === 'navigate') {                                                // pages: network only, never cached
    e.respondWith(fetch(req).catch(() => new Response(OFFLINE, {headers: {'Content-Type': 'text/html; charset=utf-8'}})));
  }
});
""" % {"offline": json.dumps(OFFLINE_PAGE)}


def register(app: FastAPI) -> None:
    @app.get("/manifest.webmanifest", include_in_schema=False)
    def manifest():
        return JSONResponse(MANIFEST, media_type="application/manifest+json", headers={"Cache-Control": "public, max-age=3600"})

    @app.get("/sw.js", include_in_schema=False)
    def service_worker():
        return Response(SERVICE_WORKER, media_type="text/javascript", headers={"Cache-Control": "no-cache"})
