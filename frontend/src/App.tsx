import { Routes, Route, Navigate, useLocation, useNavigate } from "react-router-dom";
import { useState, useCallback, useEffect } from "react";
import { useAuth } from "./hooks/useAuth";
import { usePlayer } from "./contexts/PlayerContext";
import { useNativeNotifications } from "./hooks/useNativeNotifications";
import { useAppUpdateNotification } from "./hooks/useAppUpdateNotification";
import { DEEPLINK_NAV_EVENT } from "./deepLinks";
import Navbar from "./components/Navbar";
import MiniPlayer from "./components/MiniPlayer";
import AppUpdateBanner from "./components/AppUpdateBanner";
import PlayerPage from "./pages/Player";
import Login from "./pages/Login";
import ChangePassword from "./pages/ChangePassword";
import SetEmail from "./pages/SetEmail";
import Home from "./pages/Home";
import SearchResults from "./pages/SearchResults";
import BookDetailPage from "./pages/BookDetail";
import SeriesPage from "./pages/SeriesPage";
import ShelfPage from "./pages/ShelfPage";
import GenreHubPage from "./pages/GenreHubPage";
import RequestsPage from "./pages/Requests";
import AdminPage from "./pages/Admin";
import InstanceSetup from "./pages/InstanceSetup";
import MyLibrary from "./pages/MyLibrary";
import LibraryBookDetail from "./pages/LibraryBookDetail";
import ShareBookDetail from "./pages/ShareBookDetail";
import LibraryEbookDetail from "./pages/LibraryEbookDetail";
import Ereader from "./pages/Ereader";
import Settings from "./pages/Settings";
import Onboarding from "./pages/Onboarding";
import JoinInvite from "./pages/JoinInvite";
import LibrariesPage from "./pages/Libraries";
import ListeningHistory from "./pages/ListeningHistory";
import ListenRoute from "./pages/ListenRoute";
import OfflineBanner from "./components/OfflineBanner";
import OfflineUnlockSetupPrompt from "./components/OfflineUnlockSetupPrompt";
import CoachMarks from "./components/CoachMarks";
import { useLibraryGroup } from "./hooks/useLibraryGroup";
import { useThemeSync } from "./theme/useThemeSync";
import { useOnlineStatus } from "./hooks/useOnlineStatus";
import { usePresenceHeartbeat } from "./hooks/usePresenceHeartbeat";
import { isLikelyOffline } from "./utils/networkStatus";

function ThemeSync() {
  useThemeSync();
  return null;
}
function authGatePath(user: { mustChangePassword: boolean; mustSetEmail: boolean }): string | null {
  if (user.mustChangePassword) return "/change-password";
  if (user.mustSetEmail) return "/set-email";
  return null;
}

function unauthHome(setupRequired: boolean): string {
  return setupRequired ? "/login" : "/libraries";
}

function ProtectedRoute({ children }: { children: React.ReactNode }) {
  const { user, isLoading, sessionReady, offlineSession, setupRequired } = useAuth();
  const online = useOnlineStatus();
  const hasLibraryToken = !!localStorage.getItem("access_token");
  const libraryQuery = useLibraryGroup(
    !!user &&
      sessionReady &&
      !user.mustChangePassword &&
      !user.mustSetEmail &&
      hasLibraryToken &&
      online &&
      !offlineSession
  );
  if (isLoading || !sessionReady) return <div className="min-h-screen flex items-center justify-center text-gray-400">Loading...</div>;
  if (!hasLibraryToken || !user) return <Navigate to={unauthHome(setupRequired)} />;
  const gate = authGatePath(user);
  // Offline unlock restores must_* flags from cache — don't block reading offline.
  if (gate && online && !offlineSession && !isLikelyOffline()) return <Navigate to={gate} />;
  if (
    libraryQuery.data &&
    libraryQuery.data.library === null &&
    online &&
    !offlineSession
  ) {
    return <Navigate to="/onboarding" />;
  }
  return <>{children}</>;
}

function OnboardingRoute({ children }: { children: React.ReactNode }) {
  const { user, isLoading, setupRequired } = useAuth();
  if (isLoading) return <div className="min-h-screen flex items-center justify-center text-gray-400">Loading...</div>;
  if (!user) return <Navigate to={unauthHome(setupRequired)} />;
  const gate = authGatePath(user);
  if (gate) return <Navigate to={gate} />;
  return <>{children}</>;
}

/** Zero users → force first-run admin create on /login (not invite-only /libraries). */
function FirstRunRedirect({ children }: { children: React.ReactNode }) {
  const { user, sessionReady, setupRequired } = useAuth();
  if (!sessionReady) {
    return <div className="min-h-screen flex items-center justify-center text-gray-400">Loading...</div>;
  }
  if (setupRequired && !user) return <Navigate to="/login" replace />;
  return <>{children}</>;
}

function CatchAllHome() {
  const { user, sessionReady, setupRequired } = useAuth();
  if (!sessionReady) {
    return <div className="min-h-screen flex items-center justify-center text-gray-400">Loading...</div>;
  }
  return <Navigate to={user ? "/libraries" : unauthHome(setupRequired)} replace />;
}

function AdminRoute({ children }: { children: React.ReactNode }) {
  const { user, isLoading } = useAuth();
  if (isLoading) return <div className="min-h-screen flex items-center justify-center text-gray-400">Loading...</div>;
  if (!user) return <Navigate to="/login" />;
  const gate = authGatePath(user);
  if (gate) return <Navigate to={gate} />;
  if (user.role !== "admin") return <Navigate to="/my-library" />;
  return <>{children}</>;
}

function DeepLinkNavigator() {
  const navigate = useNavigate();
  useEffect(() => {
    const onNav = (ev: Event) => {
      const path = (ev as CustomEvent<{ path?: string }>).detail?.path;
      if (path) navigate(path, { replace: true });
    };
    window.addEventListener(DEEPLINK_NAV_EVENT, onNav);
    return () => window.removeEventListener(DEEPLINK_NAV_EVENT, onNav);
  }, [navigate]);
  return null;
}

export default function App() {
  const { user, sessionReady, offlineSession } = useAuth();
  const { nowPlaying, expanded } = usePlayer();
  const location = useLocation();

  const authReady =
    !!user && sessionReady && !user.mustChangePassword && !user.mustSetEmail;
  usePresenceHeartbeat(authReady && !offlineSession);
  useNativeNotifications(authReady);
  const {
    pendingUpdate,
    downloading: appUpdateDownloading,
    downloadUpdate,
    dismissPending,
  } = useAppUpdateNotification(authReady);

  const [genreMobileOpen, setGenreMobileOpen] = useState(false);
  const [genreActiveCount, setGenreActiveCount] = useState(0);

  const handleGenreToggle = useCallback(() => setGenreMobileOpen((v) => !v), []);
  const handleGenreMobileClose = useCallback(() => setGenreMobileOpen(false), []);

  const showGenreButton =
    location.pathname === "/" ||
    location.pathname === "/search" ||
    location.pathname.startsWith("/genre/") ||
    location.pathname.startsWith("/shelf/");

  return (
    <div className={`min-h-screen bg-gray-950 overflow-x-clip w-full max-w-[100vw] ${nowPlaying && !expanded ? "pb-[calc(5rem+env(safe-area-inset-bottom,0px))]" : ""}`}>
      {pendingUpdate && (
        <AppUpdateBanner
          update={pendingUpdate}
          downloading={appUpdateDownloading}
          onDismiss={dismissPending}
          onDownload={() => void downloadUpdate()}
        />
      )}
      {user &&
        !user.mustChangePassword &&
        !user.mustSetEmail &&
        location.pathname !== "/libraries" &&
        !location.pathname.startsWith("/read/") && (
        <Navbar />
      )}
      {user &&
        !user.mustChangePassword &&
        !user.mustSetEmail &&
        location.pathname !== "/libraries" && <OfflineBanner />}
      {user && !user.mustChangePassword && !user.mustSetEmail && (
        <OfflineUnlockSetupPrompt />
      )}
      {user && !user.mustChangePassword && !user.mustSetEmail && <CoachMarks />}
      {expanded && <PlayerPage />}
      <ThemeSync />
      <DeepLinkNavigator />
      <Routes>
        <Route path="/login" element={<Login />} />
        <Route path="/share/:token" element={<ShareBookDetail />} />
        {/* Legacy approval-flow URLs → invite-only join */}
        <Route path="/request-account" element={<Navigate to="/join" replace />} />
        <Route path="/account-status" element={<Navigate to="/join" replace />} />
        <Route path="/change-password" element={<ChangePassword />} />
        <Route path="/set-email" element={<SetEmail />} />
        <Route
          path="/join/:code"
          element={
            <FirstRunRedirect>
              <JoinInvite />
            </FirstRunRedirect>
          }
        />
        <Route
          path="/join"
          element={
            <FirstRunRedirect>
              <JoinInvite />
            </FirstRunRedirect>
          }
        />
        <Route
          path="/libraries"
          element={
            <FirstRunRedirect>
              <LibrariesPage />
            </FirstRunRedirect>
          }
        />
        <Route
          path="/onboarding"
          element={
            <OnboardingRoute>
              <Onboarding />
            </OnboardingRoute>
          }
        />
        <Route
          path="/"
          element={
            <ProtectedRoute>
              <Home
                genreMobileOpen={genreMobileOpen}
                onGenreMobileClose={handleGenreMobileClose}
                onGenreToggle={showGenreButton ? handleGenreToggle : undefined}
                genreActiveCount={genreActiveCount}
                onActiveCountChange={setGenreActiveCount}
              />
            </ProtectedRoute>
          }
        />
        <Route
          path="/search"
          element={
            <ProtectedRoute>
              <SearchResults
                genreMobileOpen={genreMobileOpen}
                onGenreMobileClose={handleGenreMobileClose}
                onGenreToggle={showGenreButton ? handleGenreToggle : undefined}
                genreActiveCount={genreActiveCount}
                onActiveCountChange={setGenreActiveCount}
              />
            </ProtectedRoute>
          }
        />
        <Route
          path="/book/*"
          element={
            <ProtectedRoute>
              <BookDetailPage />
            </ProtectedRoute>
          }
        />
        <Route
          path="/series/*"
          element={
            <ProtectedRoute>
              <SeriesPage />
            </ProtectedRoute>
          }
        />
        <Route
          path="/shelf/:slug"
          element={
            <ProtectedRoute>
              <ShelfPage
                genreMobileOpen={genreMobileOpen}
                onGenreMobileClose={handleGenreMobileClose}
                onGenreToggle={showGenreButton ? handleGenreToggle : undefined}
              />
            </ProtectedRoute>
          }
        />
        <Route
          path="/genre/:slug"
          element={
            <ProtectedRoute>
              <GenreHubPage
                genreMobileOpen={genreMobileOpen}
                onGenreMobileClose={handleGenreMobileClose}
                onGenreToggle={showGenreButton ? handleGenreToggle : undefined}
              />
            </ProtectedRoute>
          }
        />
        <Route
          path="/my-library"
          element={
            <ProtectedRoute>
              <MyLibrary />
            </ProtectedRoute>
          }
        />
        <Route
          path="/library/abs/:itemId"
          element={
            <ProtectedRoute>
              <LibraryBookDetail />
            </ProtectedRoute>
          }
        />
        <Route
          path="/library/ebook/:seriesId"
          element={
            <ProtectedRoute>
              <LibraryEbookDetail />
            </ProtectedRoute>
          }
        />
        <Route
          path="/read/:chapterId"
          element={
            <ProtectedRoute>
              <Ereader />
            </ProtectedRoute>
          }
        />
        <Route
          path="/downloads"
          element={
            <ProtectedRoute>
              <RequestsPage />
            </ProtectedRoute>
          }
        />
        <Route
          path="/requests"
          element={<Navigate to="/downloads" replace />}
        />
        <Route
          path="/history"
          element={
            <ProtectedRoute>
              <ListeningHistory />
            </ProtectedRoute>
          }
        />
        <Route
          path="/listen"
          element={
            <ProtectedRoute>
              <ListenRoute />
            </ProtectedRoute>
          }
        />
        <Route
          path="/settings"
          element={
            <ProtectedRoute>
              <Settings />
            </ProtectedRoute>
          }
        />
        <Route
          path="/admin"
          element={
            <AdminRoute>
              <AdminPage />
            </AdminRoute>
          }
        />
        <Route
          path="/admin/setup"
          element={
            <AdminRoute>
              <InstanceSetup />
            </AdminRoute>
          }
        />
        <Route
          path="*"
          element={<CatchAllHome />}
        />
      </Routes>
      {!expanded && <MiniPlayer />}
    </div>
  );
}
