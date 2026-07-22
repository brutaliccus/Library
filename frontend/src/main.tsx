import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { AuthProvider } from "./hooks/useAuth";
import { ToastProvider } from "./contexts/ToastContext";
import { PlayerProvider } from "./contexts/PlayerContext";
import ToastContainer from "./components/Toast";
import UpdateBanner from "./components/UpdateBanner";
import App from "./App";
import { bootstrapThemeFromCache } from "./theme/themes";
import {
  LIBRARY_COLLECTION_PREFIXES,
  clearLegacyShelfPersist,
  shelfPersistKey,
} from "./utils/shelfQueryCache";
import "./index.css";

// Paint last-chosen theme before React mounts (avoids ocean→real bounce).
bootstrapThemeFromCache();

const queryClient = new QueryClient({
  defaultOptions: {
    queries: { retry: 1, refetchOnWindowFocus: false },
  },
});

// --- Lightweight query-cache persistence -------------------------------------
// A reopened PWA (killed after idle) starts with an empty cache and re-fetches
// everything against a cold server — the "takes ages" cold start. We persist a
// whitelist of slow-changing shelves to localStorage and restore them on boot,
// so Home / My Library paint instantly and revalidate in the background
// (stale-while-revalidate via each query's staleTime). Never auth tokens or
// "continue listening" progress — only shelf/collection payloads.
//
// v5: origin-scoped keys so multi-library devices never mix catalogs.
const PERSIST_PREFIXES = [
  "trending-books",
  "new-releases",
  "home-shelves",
  "category-carousel",
  "genres",
  "curated-slugs",
  // My Library — slow ABS/Kavita/PC collection payloads
  // Series/author shelves are derived client-side from these collections (offline-safe).
  ...LIBRARY_COLLECTION_PREFIXES,
];
const PERSIST_MAX_AGE = 24 * 60 * 60 * 1000; // 24h
const LIBRARY_COLLECTION_SET = new Set<string>(LIBRARY_COLLECTION_PREFIXES);

/** Drop persisted shelf rows that still have blank/stub covers (stale after enrich fixes). */
function shelfLooksCoverBroken(data: unknown): boolean {
  const books = (data as { books?: Array<{ coverUrl?: string }> } | undefined)?.books;
  if (!Array.isArray(books) || books.length === 0) return false;
  const blank = books.filter((b) => !(b?.coverUrl || "").trim()).length;
  return blank / books.length >= 0.25;
}

try {
  clearLegacyShelfPersist();
  const raw = localStorage.getItem(shelfPersistKey());
  if (raw) {
    const saved = JSON.parse(raw) as { t: number; entries: [unknown, unknown][] };
    if (saved && Date.now() - saved.t < PERSIST_MAX_AGE && Array.isArray(saved.entries)) {
      for (const [key, data] of saved.entries) {
        const first = Array.isArray(key) ? String(key[0]) : "";
        if (
          (first === "trending-books" || first === "new-releases") &&
          shelfLooksCoverBroken(data)
        ) {
          continue; // force network fetch for broken shelf snapshots
        }
        // Collection shelves: paint instantly from disk, but mark stale so a
        // background refetch always replaces the full list (drops orphans).
        const updatedAt = LIBRARY_COLLECTION_SET.has(first) ? 0 : saved.t;
        queryClient.setQueryData(key as readonly unknown[], data, { updatedAt });
      }
    }
  }
} catch {
  // ignore corrupt/oversized cache
}

let _persistTimer: number | undefined;
queryClient.getQueryCache().subscribe(() => {
  if (_persistTimer !== undefined) return;
  _persistTimer = window.setTimeout(() => {
    _persistTimer = undefined;
    try {
      const entries: [unknown, unknown][] = [];
      for (const q of queryClient.getQueryCache().getAll()) {
        const first = Array.isArray(q.queryKey) ? String(q.queryKey[0]) : "";
        if (
          PERSIST_PREFIXES.includes(first) &&
          q.state.status === "success" &&
          q.state.data !== undefined
        ) {
          // Store a plain JSON clone so later in-place mutations cannot leak
          // into localStorage, and restore always gets a full replacement array.
          entries.push([q.queryKey, JSON.parse(JSON.stringify(q.state.data))]);
        }
      }
      // Always rewrite (including empty) so removeQueries drops orphans from disk.
      localStorage.setItem(shelfPersistKey(), JSON.stringify({ t: Date.now(), entries }));
    } catch {
      // localStorage full/unavailable — skip persistence this round
    }
  }, 1500);
});

async function start() {
  // Native: apply cold-start invite deep link before AuthProvider checks the API.
  try {
    const { consumeLaunchDeepLink, registerDeepLinkListener } = await import("./deepLinks");
    const launchPath = await consumeLaunchDeepLink();
    void registerDeepLinkListener();
    if (launchPath && typeof window !== "undefined") {
      // Ensure first paint routes to /join/CODE (SPA may have opened at /).
      const here = window.location.pathname + window.location.search;
      if (!here.startsWith("/join")) {
        window.history.replaceState(null, "", launchPath);
      }
    }
  } catch {
    // web / plugin unavailable
  }

  ReactDOM.createRoot(document.getElementById("root")!).render(
    <React.StrictMode>
      <BrowserRouter>
        <QueryClientProvider client={queryClient}>
          <AuthProvider>
            <ToastProvider>
              <PlayerProvider>
                <App />
                <UpdateBanner />
                <ToastContainer />
              </PlayerProvider>
            </ToastProvider>
          </AuthProvider>
        </QueryClientProvider>
      </BrowserRouter>
    </React.StrictMode>
  );
}

void start();
