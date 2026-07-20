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
import "./index.css";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: { retry: 1, refetchOnWindowFocus: false },
  },
});

// --- Lightweight query-cache persistence -------------------------------------
// A reopened PWA (killed after idle) starts with an empty cache and re-fetches
// everything against a cold server — the "takes ages" cold start. We persist a
// whitelist of slow-changing shelves to localStorage and restore them on boot,
// so Home paints instantly and revalidates in the background (stale-while-
// revalidate via each query's staleTime). Only cacheable, user-agnostic shelves
// are stored — never auth or "continue listening".
const PERSIST_KEY = "rq-shelf-cache-v3";
const PERSIST_PREFIXES = [
  "trending-books",
  "new-releases",
  "home-shelves",
  "category-carousel",
  "genres",
  "curated-slugs",
];
const PERSIST_MAX_AGE = 24 * 60 * 60 * 1000; // 24h

/** Drop persisted shelf rows that still have blank/stub covers (stale after enrich fixes). */
function shelfLooksCoverBroken(data: unknown): boolean {
  const books = (data as { books?: Array<{ coverUrl?: string }> } | undefined)?.books;
  if (!Array.isArray(books) || books.length === 0) return false;
  const blank = books.filter((b) => !(b?.coverUrl || "").trim()).length;
  return blank / books.length >= 0.25;
}

try {
  // Drop prior persist generations that froze blank OL stub covers for 24h.
  localStorage.removeItem("rq-shelf-cache-v2");
  const raw = localStorage.getItem(PERSIST_KEY);
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
        // Restore with the ORIGINAL timestamp so staleTime still triggers a
        // background refresh when appropriate (instant paint, fresh data soon).
        queryClient.setQueryData(key as readonly unknown[], data, { updatedAt: saved.t });
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
          entries.push([q.queryKey, q.state.data]);
        }
      }
      if (entries.length) {
        localStorage.setItem(PERSIST_KEY, JSON.stringify({ t: Date.now(), entries }));
      }
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
