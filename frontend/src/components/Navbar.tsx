import { Link, useLocation, useNavigate } from "react-router-dom";
import { useAuth } from "../hooks/useAuth";
import { usePlayer } from "../contexts/PlayerContext";
import { useOnlineStatus } from "../hooks/useOnlineStatus";
import { useState, FormEvent, useEffect } from "react";
import { BookOpen, Home, List, Shield, LogOut, Search, Headphones, Library, LayoutGrid, SlidersHorizontal, Lock, Unlock, Settings } from "lucide-react";

interface Props {
  onGenreToggle?: () => void;
  genreActiveCount?: number;
}

export default function Navbar({ onGenreToggle, genreActiveCount = 0 }: Props) {
  const { user, logout, offlineSession } = useAuth();
  const { nowPlaying, isPlaying, setExpanded } = usePlayer();
  const online = useOnlineStatus();
  const location = useLocation();
  const navigate = useNavigate();
  const [searchValue, setSearchValue] = useState("");
  const [rotationLocked, setRotationLocked] = useState(!!document.fullscreenElement);
  const [rotationLockSupported, setRotationLockSupported] = useState(false);
  const onlineOnly = online && !offlineSession;

  useEffect(() => {
    const orient = screen.orientation as ScreenOrientation & { lock?: (mode: string) => Promise<void> };
    const supported = typeof orient?.lock === "function";
    setRotationLockSupported(supported);
    const onFullscreenChange = () => setRotationLocked(!!document.fullscreenElement);
    document.addEventListener("fullscreenchange", onFullscreenChange);
    return () => document.removeEventListener("fullscreenchange", onFullscreenChange);
  }, []);

  const handleRotationLock = async () => {
    if (document.fullscreenElement) {
      await document.exitFullscreen();
      return;
    }
    try {
      await document.documentElement.requestFullscreen();
      const orient = screen.orientation as ScreenOrientation & { lock?: (mode: string) => Promise<void> };
      if (typeof orient?.lock === "function") {
        await orient.lock("portrait");
      }
    } catch {
      // Fullscreen or lock may require user gesture; some browsers restrict
    }
  };

  if (!user) return null;

  const links = [
    { to: "/libraries", label: "Libraries", icon: LayoutGrid },
    { to: "/", label: "Home", icon: Home, onlineOnly: true },
    { to: "/my-library", label: "My Library", icon: Library },
    { to: "/requests", label: "My Requests", icon: List, onlineOnly: true },
    ...(user.role === "admin"
      ? [{ to: "/admin", label: "Admin", icon: Shield, onlineOnly: true }]
      : []),
  ];

  const isActive = (path: string) =>
    path === "/"
      ? location.pathname === "/"
      : location.pathname.startsWith(path);

  const handleSearch = (e: FormEvent) => {
    e.preventDefault();
    const q = searchValue.trim();
    if (q.length >= 2) {
      navigate(`/search?q=${encodeURIComponent(q)}`);
      setSearchValue("");
    }
  };

  return (
    <nav className="bg-gray-900 border-b border-gray-800 sticky top-0 z-50 pt-[env(safe-area-inset-top,0px)]">
      <div className="px-4 lg:px-6 flex items-center justify-between h-14 gap-3">
        <Link to="/" className="flex items-center gap-2 text-brand-400 font-bold text-lg shrink-0">
          <BookOpen size={22} />
          <span className="hidden sm:inline">Library</span>
        </Link>

        {onlineOnly ? (
          <form onSubmit={handleSearch} className="relative flex-1 max-w-md hidden sm:block">
            <Search
              size={16}
              className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-500"
            />
            <input
              type="text"
              value={searchValue}
              onChange={(e) => setSearchValue(e.target.value)}
              placeholder="Search books..."
              className="w-full pl-9 pr-3 py-1.5 bg-gray-800 border border-gray-700 rounded-lg text-sm text-gray-100 focus:outline-none focus:ring-1 focus:ring-brand-500 focus:border-transparent placeholder:text-gray-500"
            />
          </form>
        ) : (
          <div className="flex-1 max-w-md hidden sm:block text-xs text-gray-500 px-2">
            Search unavailable offline
          </div>
        )}

        <div className="flex items-center gap-1">
          {onGenreToggle && onlineOnly && (
            <button
              onClick={onGenreToggle}
              className="lg:hidden flex items-center gap-1.5 px-3 py-2 rounded-lg text-sm font-medium text-gray-400 hover:bg-gray-800 hover:text-gray-200 transition-colors"
              title="Genres"
            >
              <SlidersHorizontal size={16} />
              <span className="hidden sm:inline">Genres</span>
              {genreActiveCount > 0 && (
                <span className="px-1.5 py-0.5 bg-brand-600 text-white text-[10px] font-bold rounded-full leading-none">
                  {genreActiveCount}
                </span>
              )}
            </button>
          )}

          {links.map(({ to, label, icon: Icon, onlineOnly: needsOnline }) => {
            if (needsOnline && !onlineOnly) {
              return (
                <span
                  key={to}
                  title="Unavailable offline"
                  className="flex items-center gap-1.5 px-3 py-2 rounded-lg text-sm font-medium text-gray-600 cursor-not-allowed"
                >
                  <Icon size={16} />
                  <span className="hidden md:inline">{label}</span>
                </span>
              );
            }
            return (
              <Link
                key={to}
                to={to}
                className={`flex items-center gap-1.5 px-3 py-2 rounded-lg text-sm font-medium transition-colors ${
                  isActive(to)
                    ? "bg-brand-600/20 text-brand-400"
                    : "text-gray-400 hover:bg-gray-800 hover:text-gray-200"
                }`}
              >
                <Icon size={16} />
                <span className="hidden md:inline">{label}</span>
              </Link>
            );
          })}

          {nowPlaying && (
            <button
              onClick={() => setExpanded(true)}
              className="flex items-center gap-1.5 px-3 py-2 rounded-lg text-sm font-medium text-emerald-400 hover:bg-gray-800 transition-colors"
              title={`Now playing: ${nowPlaying.title}`}
            >
              <Headphones size={16} className={isPlaying ? "animate-pulse" : ""} />
              <span className="hidden lg:inline truncate max-w-[120px]">
                {nowPlaying.title}
              </span>
            </button>
          )}

          <div className="ml-2 pl-2 border-l border-gray-700 flex items-center gap-2">
            {rotationLockSupported && (
              <button
                onClick={handleRotationLock}
                className={`p-2 rounded-lg transition-colors ${
                  rotationLocked
                    ? "text-brand-400 hover:bg-gray-800"
                    : "text-gray-500 hover:text-gray-200 hover:bg-gray-800"
                }`}
                title={rotationLocked ? "Unlock rotation (exit fullscreen)" : "Lock rotation to portrait"}
              >
                {rotationLocked ? <Lock size={16} /> : <Unlock size={16} />}
              </button>
            )}
            <span className="text-sm text-gray-400 hidden md:inline">{user.username}</span>
            <Link
              to="/settings"
              className={`p-2 rounded-lg transition-colors ${
                isActive("/settings")
                  ? "text-brand-400 bg-brand-600/20"
                  : "text-gray-500 hover:text-gray-200 hover:bg-gray-800"
              }`}
              title="Settings"
            >
              <Settings size={16} />
            </Link>
            <button
              onClick={logout}
              className="p-2 text-gray-500 hover:text-red-400 transition-colors rounded-lg hover:bg-gray-800"
              title="Log out"
            >
              <LogOut size={16} />
            </button>
          </div>
        </div>
      </div>
    </nav>
  );
}
