/** Bump when fetch/caching behavior changes so old caches are dropped. */
const CACHE_VERSION = 10;
const CACHE_NAME = `audiobook-library-v${CACHE_VERSION}`;
/** Downloaded audiobook tracks (populated by the app, served here). Survives
 * SW updates — cleared per-book when a book is finished or progress cleared. */
const AUDIO_CACHE = "audio-tracks-v1";
/** Downloaded ebook files (PDF/EPUB source) for offline re-read. */
const EBOOK_CACHE = "ebook-files-v1";

self.addEventListener("install", () => {
  // Wait for the user to tap "Update" (SKIP_WAITING) before taking over.
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) =>
        Promise.all(
          keys
            .filter((key) => key !== CACHE_NAME && key !== AUDIO_CACHE && key !== EBOOK_CACHE)
            .map((key) => caches.delete(key))
        )
      )
      .then(() => self.clients.claim())
  );
});

self.addEventListener("message", (event) => {
  if (event.data?.type === "SKIP_WAITING") {
    self.skipWaiting();
  }
});

self.addEventListener("push", (event) => {
  if (!event.data) return;
  let payload = { title: "Library", body: "", url: "/" };
  try {
    payload = { ...payload, ...event.data.json() };
  } catch {
    payload.body = event.data.text();
  }
  const options = {
    body: payload.body,
    icon: "/icon-192.png",
    badge: "/icon-192.png",
    tag: payload.type || "library",
    data: { url: payload.url || "/" },
  };
  event.waitUntil(self.registration.showNotification(payload.title, options));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const url = event.notification.data?.url || "/";
  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((clients) => {
      if (clients.length > 0) {
        const c = clients[0];
        c.navigate(url);
        c.focus();
      } else if (self.clients.openWindow) {
        self.clients.openWindow(url);
      }
    })
  );
});

function shouldBypassCache(url) {
  return (
    url.includes("/api/") ||
    url.includes("/ws") ||
    url.includes("/stream/")
  );
}

function isHashedAsset(url) {
  try {
    const path = new URL(url).pathname;
    return path.startsWith("/assets/");
  } catch {
    return false;
  }
}

/** Cache only Vite-hashed bundles; app shell always comes from the network when online. */
async function assetNetworkFirst(request) {
  try {
    const response = await fetch(request);
    if (response.ok && response.type === "basic") {
      const cache = await caches.open(CACHE_NAME);
      await cache.put(request, response.clone());
    }
    return response;
  } catch {
    const cached = await caches.match(request);
    if (cached) return cached;
    throw new Error("offline");
  }
}

/** Stable stream-proxy audio URL (debrid or ABS). */
function isAudioProxyUrl(url) {
  try {
    const path = new URL(url).pathname;
    return (
      path.startsWith("/api/stream/rd/proxy/") ||
      path.startsWith("/api/stream/abs/proxy/audio/")
    );
  } catch {
    return false;
  }
}

/** Stable reader file URL (PDF or EPUB source). */
function isEbookReaderUrl(url) {
  try {
    const path = new URL(url).pathname;
    return /^\/api\/library\/reader\/\d+\/(pdf|file)$/.test(path);
  } catch {
    return false;
  }
}

/** Normalize cache keys (pathname only, no query/hash). */
function normalizeCacheUrl(url) {
  try {
    const u = new URL(url);
    return u.origin + u.pathname;
  } catch {
    return url;
  }
}

/** Find a cached audio response by normalized URL. */
async function matchAudioCache(cache, request) {
  const key = normalizeCacheUrl(request.url);
  let cached = await cache.match(key);
  if (cached) return cached;
  return cache.match(request.url);
}

/** Serve downloaded audio from the local cache (with Range support) so
 * paused/resumed books play instantly without hitting the debrid service.
 * Falls through to the network when the track isn't downloaded. */
async function audioCacheFirst(request) {
  const cache = await caches.open(AUDIO_CACHE);
  const cached = await matchAudioCache(cache, request);
  if (!cached) return fetch(request);

  const range = request.headers.get("range");
  if (!range) return cached.clone();

  const m = /bytes=(\d+)-(\d*)/.exec(range);
  if (!m) return cached.clone();

  const blob = await cached.clone().blob();
  const size = blob.size;
  const start = Number(m[1]);
  const end = m[2] ? Math.min(Number(m[2]), size - 1) : size - 1;
  if (start >= size) {
    return new Response(null, {
      status: 416,
      headers: { "Content-Range": `bytes */${size}` },
    });
  }
  return new Response(blob.slice(start, end + 1), {
    status: 206,
    headers: {
      "Content-Type": cached.headers.get("content-type") || "audio/mpeg",
      "Content-Range": `bytes ${start}-${end}/${size}`,
      "Content-Length": String(end - start + 1),
      "Accept-Ranges": "bytes",
    },
  });
}

/** Serve cached ebook files with Range support (PDF.js / chunked download). */
async function ebookCacheFirst(request) {
  const cache = await caches.open(EBOOK_CACHE);
  const key = normalizeCacheUrl(request.url);
  let cached = await cache.match(key);
  if (!cached) cached = await cache.match(request.url);
  if (!cached) return fetch(request);

  const range = request.headers.get("range");
  if (!range) return cached.clone();

  const m = /bytes=(\d+)-(\d*)/.exec(range);
  if (!m) return cached.clone();

  const blob = await cached.clone().blob();
  const size = blob.size;
  const start = Number(m[1]);
  const end = m[2] ? Math.min(Number(m[2]), size - 1) : size - 1;
  if (start >= size) {
    return new Response(null, {
      status: 416,
      headers: { "Content-Range": `bytes */${size}` },
    });
  }
  const contentType = cached.headers.get("content-type") || "application/octet-stream";
  return new Response(blob.slice(start, end + 1), {
    status: 206,
    headers: {
      "Content-Type": contentType,
      "Content-Range": `bytes ${start}-${end}/${size}`,
      "Content-Length": String(end - start + 1),
      "Accept-Ranges": "bytes",
    },
  });
}

self.addEventListener("fetch", (event) => {
  const { request } = event;
  if (request.method !== "GET") {
    return;
  }

  // Never intercept document navigations. Re-fetching the SPA shell from the
  // SW breaks auth redirects and surfaces as "network error" / Failed to fetch
  // when the server returns 401 or the connection blips.
  if (request.mode === "navigate") {
    return;
  }

  if (isAudioProxyUrl(request.url)) {
    event.respondWith(audioCacheFirst(request));
    return;
  }

  if (isEbookReaderUrl(request.url)) {
    event.respondWith(ebookCacheFirst(request));
    return;
  }

  if (shouldBypassCache(request.url)) {
    return;
  }

  if (isHashedAsset(request.url)) {
    event.respondWith(assetNetworkFirst(request));
    return;
  }

  // Icons, manifest, etc. — pass through without respondWith so the browser
  // handles errors normally (no uncaught promise in the SW).
});
