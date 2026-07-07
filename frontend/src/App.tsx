import { Routes, Route, Navigate, useLocation } from "react-router-dom";
import { useState, useCallback } from "react";
import { useAuth } from "./hooks/useAuth";
import { usePlayer } from "./contexts/PlayerContext";
import { useNativeNotifications } from "./hooks/useNativeNotifications";
import Navbar from "./components/Navbar";
import MiniPlayer from "./components/MiniPlayer";
import PlayerPage from "./pages/Player";
import Login from "./pages/Login";
import RequestAccount from "./pages/RequestAccount";
import AccountStatus from "./pages/AccountStatus";
import ChangePassword from "./pages/ChangePassword";
import Home from "./pages/Home";
import SearchResults from "./pages/SearchResults";
import BookDetailPage from "./pages/BookDetail";
import RequestsPage from "./pages/Requests";
import AdminPage from "./pages/Admin";
import MyLibrary from "./pages/MyLibrary";
import LibraryBookDetail from "./pages/LibraryBookDetail";
import Ereader from "./pages/Ereader";
import Settings from "./pages/Settings";
import Onboarding from "./pages/Onboarding";
import { useLibraryGroup } from "./hooks/useLibraryGroup";

function ProtectedRoute({ children }: { children: React.ReactNode }) {
  const { user, isLoading, sessionReady } = useAuth();
  const libraryQuery = useLibraryGroup(!!user && sessionReady && !user.mustChangePassword);
  if (isLoading || !sessionReady) return <div className="min-h-screen flex items-center justify-center text-gray-400">Loading...</div>;
  if (!user) return <Navigate to="/login" />;
  if (user.mustChangePassword) return <Navigate to="/change-password" />;
  // New accounts pick "create or join a library" before using the app
  if (libraryQuery.data && libraryQuery.data.library === null) return <Navigate to="/onboarding" />;
  return <>{children}</>;
}

function OnboardingRoute({ children }: { children: React.ReactNode }) {
  const { user, isLoading } = useAuth();
  if (isLoading) return <div className="min-h-screen flex items-center justify-center text-gray-400">Loading...</div>;
  if (!user) return <Navigate to="/login" />;
  if (user.mustChangePassword) return <Navigate to="/change-password" />;
  return <>{children}</>;
}

function AdminRoute({ children }: { children: React.ReactNode }) {
  const { user, isLoading } = useAuth();
  if (isLoading) return <div className="min-h-screen flex items-center justify-center text-gray-400">Loading...</div>;
  if (!user) return <Navigate to="/login" />;
  if (user.mustChangePassword) return <Navigate to="/change-password" />;
  if (user.role !== "admin") return <Navigate to="/" />;
  return <>{children}</>;
}

export default function App() {
  const { user, sessionReady } = useAuth();
  const { nowPlaying, expanded } = usePlayer();
  const location = useLocation();

  useNativeNotifications(!!user && sessionReady && !user.mustChangePassword);

  const [genreMobileOpen, setGenreMobileOpen] = useState(false);
  const [genreActiveCount, setGenreActiveCount] = useState(0);

  const handleGenreToggle = useCallback(() => setGenreMobileOpen((v) => !v), []);
  const handleGenreMobileClose = useCallback(() => setGenreMobileOpen(false), []);

  const showGenreButton = location.pathname === "/" || location.pathname === "/search";

  return (
    <div className={`min-h-screen bg-gray-950 ${nowPlaying && !expanded ? "pb-[calc(5rem+env(safe-area-inset-bottom,0px))]" : ""}`}>
      {user && !user.mustChangePassword && (
        <Navbar
          onGenreToggle={showGenreButton ? handleGenreToggle : undefined}
          genreActiveCount={showGenreButton ? genreActiveCount : 0}
        />
      )}
      {expanded && <PlayerPage />}
      <Routes>
        <Route path="/login" element={<Login />} />
        <Route path="/request-account" element={<RequestAccount />} />
        <Route path="/account-status" element={<AccountStatus />} />
        <Route path="/change-password" element={<ChangePassword />} />
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
                onActiveCountChange={setGenreActiveCount}
              />
            </ProtectedRoute>
          }
        />
        <Route
          path="/book/:volumeId"
          element={
            <ProtectedRoute>
              <BookDetailPage />
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
          path="/read/:chapterId"
          element={
            <ProtectedRoute>
              <Ereader />
            </ProtectedRoute>
          }
        />
        <Route
          path="/requests"
          element={
            <ProtectedRoute>
              <RequestsPage />
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
        <Route path="*" element={<Navigate to="/" />} />
      </Routes>
      {!expanded && <MiniPlayer />}
    </div>
  );
}
