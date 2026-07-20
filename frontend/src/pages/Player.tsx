import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { usePlayer } from "../contexts/PlayerContext";
import {
  X,
  Minimize2,
  Volume2,
  BookOpen,
  Menu,
} from "lucide-react";
import AudiobookTransport from "../components/AudiobookTransport";
import PlaybackScrubber from "../components/PlaybackScrubber";
import CoverImage from "../components/CoverImage";
import {
  chapterNavAvailability,
  currentChapterLabel,
  indexOfChapterAtTime,
  playbackScope,
  seekTimeFromScope,
} from "../utils/playerNav";

const SPEED_OPTIONS = [0.5, 0.75, 1, 1.25, 1.5, 1.75, 2];
const SLEEP_TIMER_MINUTES = [5, 10, 15, 20, 25, 30, 60] as const;
const SKIP_SECONDS = 15;

function formatTime(s: number): string {
  if (!s || !isFinite(s)) return "0:00:00";
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = Math.floor(s % 60);
  const pad = (n: number) => n.toString().padStart(2, "0");
  return `${h}:${pad(m)}:${pad(sec)}`;
}

function formatSleepRemaining(sec: number): string {
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = sec % 60;
  const pad = (n: number) => n.toString().padStart(2, "0");
  if (h > 0) return `${h}:${pad(m)}:${pad(s)}`;
  return `${m}:${pad(s)}`;
}

export default function PlayerPage() {
  const navigate = useNavigate();
  const {
    nowPlaying,
    isPlaying,
    currentTime,
    currentTrackIndex,
    playbackRate,
    volume,
    buffering,
    togglePlay,
    seek,
    seekRelative,
    setPlaybackRate,
    setVolume,
    setExpanded,
    dismissPlayer,
    jumpToTrack,
    sleepTimerPresetMinutes,
    sleepTimerSecondsRemaining,
    setSleepTimer,
    skipChapterPrev,
    skipChapterNext,
  } = usePlayer();

  const [chaptersMenuOpen, setChaptersMenuOpen] = useState(false);
  const activeRowRef = useRef<HTMLButtonElement | null>(null);

  useEffect(() => {
    if (!nowPlaying) {
      navigate("/", { replace: true });
    }
  }, [nowPlaying, navigate]);

  useEffect(() => {
    if (chaptersMenuOpen && activeRowRef.current) {
      activeRowRef.current.scrollIntoView({ block: "nearest", behavior: "smooth" });
    }
  }, [chaptersMenuOpen, currentTime, currentTrackIndex, nowPlaying?.absChapters?.length]);

  if (!nowPlaying) return null;

  const scope = playbackScope(nowPlaying, currentTime, currentTrackIndex);
  const scopeProgress =
    scope.duration > 0 ? (scope.position / scope.duration) * 100 : 0;
  const scopeRemaining = Math.max(0, scope.duration - scope.position);
  const { prev: canPrevChapter, next: canNextChapter } = chapterNavAvailability(
    nowPlaying,
    currentTime,
    currentTrackIndex
  );

  const chLabel = currentChapterLabel(nowPlaying, currentTime);
  const hasAbsChapters = (nowPlaying.absChapters?.length ?? 0) > 0;
  const showTrackLine = nowPlaying.tracks.length > 1;
  const activeChapterIdx = hasAbsChapters
    ? indexOfChapterAtTime(nowPlaying.absChapters!, currentTime)
    : -1;
  const drawerHasList = hasAbsChapters || nowPlaying.tracks.length > 1;

  const handleScrubFraction = (fraction: number) => {
    seek(seekTimeFromScope(scope, fraction));
  };

  const handleClose = () => {
    setExpanded(false);
    navigate(-1);
  };

  return (
    <div className="fixed inset-0 z-[100] bg-gray-950 overflow-y-auto">
      <div className="max-w-lg mx-auto px-6 pt-[calc(2rem+env(safe-area-inset-top,0px))] pb-[calc(2rem+env(safe-area-inset-bottom,0px))] flex flex-col min-h-screen">
        <div className="flex items-center justify-between mb-8 gap-2">
          <div className="flex items-center gap-1 shrink-0">
            <button
              type="button"
              onClick={() => setChaptersMenuOpen(true)}
              className="p-2 text-gray-400 hover:text-white transition-colors rounded-lg hover:bg-gray-800/80"
              title={drawerHasList ? "Chapters & navigation" : "Book navigation"}
              aria-label="Open chapters and navigation"
            >
              <Menu size={22} />
            </button>
            <button
              type="button"
              onClick={handleClose}
              className="p-2 text-gray-400 hover:text-white transition-colors"
              aria-label="Minimize player"
            >
              <Minimize2 size={20} />
            </button>
          </div>
          <span className="text-sm text-gray-500 uppercase tracking-wider font-medium text-center flex-1 truncate px-2">
            Now Playing
          </span>
          <button
            type="button"
            onClick={() => {
              dismissPlayer();
              navigate(-1);
            }}
            className="p-2 text-gray-400 hover:text-white transition-colors shrink-0"
            aria-label="Close player"
          >
            <X size={20} />
          </button>
        </div>

        <div className="flex-1 flex flex-col items-center justify-center gap-6">
          <div className="w-64 h-64 rounded-2xl overflow-hidden shadow-2xl shadow-black/50">
            <CoverImage
              src={nowPlaying.coverUrl}
              alt=""
              className="w-full h-full object-cover"
              fallback={
                <div className="w-full h-full bg-gray-800 flex items-center justify-center">
                  <BookOpen size={64} className="text-gray-700" />
                </div>
              }
            />
          </div>

          <div className="text-center w-full">
            <h1 className="text-xl font-bold text-gray-100 truncate">{nowPlaying.title}</h1>
            {nowPlaying.author && (
              <p className="text-sm text-gray-400 mt-1">{nowPlaying.author}</p>
            )}
            {hasAbsChapters && chLabel && (
              <p className="text-xs text-brand-400/90 mt-2 font-medium line-clamp-2">{chLabel}</p>
            )}
            {!hasAbsChapters && showTrackLine && (
              <p className="text-xs text-gray-600 mt-2">
                Track {currentTrackIndex + 1} of {nowPlaying.tracks.length}
                {nowPlaying.tracks[currentTrackIndex]?.title &&
                  ` — ${nowPlaying.tracks[currentTrackIndex].title}`}
              </p>
            )}
          </div>

          <div className="w-full">
            <PlaybackScrubber
              progress={scopeProgress}
              disabled={scope.duration <= 0}
              onSeekFraction={handleScrubFraction}
            />
            <div className="flex justify-between mt-1.5 text-xs text-gray-500 tabular-nums">
              <span>{formatTime(scope.position)}</span>
              <span>
                {scope.duration > 0 ? `-${formatTime(scopeRemaining)}` : "—:——:——"}
              </span>
            </div>
          </div>

          <AudiobookTransport
            variant="full"
            skipSeconds={SKIP_SECONDS}
            seekRelative={seekRelative}
            togglePlay={togglePlay}
            isPlaying={isPlaying}
            buffering={buffering}
            skipChapterPrev={skipChapterPrev}
            skipChapterNext={skipChapterNext}
            canPrevChapter={canPrevChapter}
            canNextChapter={canNextChapter}
          />

          <div className="flex items-center gap-6 w-full">
            <div className="flex items-center gap-2">
              <span className="text-xs text-gray-500">Speed</span>
              <select
                value={playbackRate}
                onChange={(e) => setPlaybackRate(parseFloat(e.target.value))}
                className="bg-gray-800 border border-gray-700 text-gray-200 text-xs rounded-lg px-2 py-1 focus:outline-none focus:ring-1 focus:ring-brand-500"
              >
                {SPEED_OPTIONS.map((s) => (
                  <option key={s} value={s}>
                    {s}x
                  </option>
                ))}
              </select>
            </div>

            <div className="flex-1 flex items-center gap-2 justify-end">
              <Volume2 size={14} className="text-gray-500 shrink-0" />
              <input
                type="range"
                min={0}
                max={1}
                step={0.05}
                value={volume}
                onChange={(e) => setVolume(parseFloat(e.target.value))}
                className="w-32 accent-brand-500 h-1"
              />
            </div>
          </div>

          <div className="w-full flex flex-col gap-2 pt-2">
            <span className="text-xs text-gray-500">Sleep timer</span>
            <div className="flex flex-wrap items-center gap-3">
              <select
                value={sleepTimerPresetMinutes ?? ""}
                onChange={(e) => {
                  const v = e.target.value;
                  if (v === "") setSleepTimer(null);
                  else setSleepTimer(Number(v));
                }}
                className="bg-gray-800 border border-gray-700 text-gray-200 text-sm rounded-lg px-3 py-2 focus:outline-none focus:ring-1 focus:ring-brand-500 min-w-[11rem]"
                aria-label="Sleep timer"
              >
                <option value="">Off</option>
                {SLEEP_TIMER_MINUTES.map((m) => (
                  <option key={m} value={m}>
                    {m} minutes
                  </option>
                ))}
              </select>
              {sleepTimerSecondsRemaining != null && sleepTimerSecondsRemaining > 0 && (
                <span className="text-sm text-brand-300 tabular-nums">
                  Pauses in {formatSleepRemaining(sleepTimerSecondsRemaining)}
                </span>
              )}
            </div>
          </div>
        </div>
      </div>

      {chaptersMenuOpen && (
        <>
          <button
            type="button"
            className="fixed inset-0 z-[110] bg-black/70 border-0 cursor-default"
            aria-label="Close menu"
            onClick={() => setChaptersMenuOpen(false)}
          />
          <aside
            className="fixed top-0 right-0 bottom-0 z-[120] w-full max-w-md bg-gray-950 border-l border-gray-800 shadow-2xl flex flex-col"
            aria-labelledby="chapters-drawer-title"
          >
            <div className="flex items-center justify-between px-4 py-4 border-b border-gray-800 shrink-0">
              <h2 id="chapters-drawer-title" className="text-lg font-semibold text-gray-100">
                Chapters & tracks
              </h2>
              <button
                type="button"
                onClick={() => setChaptersMenuOpen(false)}
                className="p-2 rounded-lg text-gray-400 hover:text-white hover:bg-gray-800"
                aria-label="Close"
              >
                <X size={22} />
              </button>
            </div>

            <div className="flex-1 overflow-y-auto px-3 py-3 pb-[env(safe-area-inset-bottom,0px)]">
              {!drawerHasList && (
                <p className="text-sm text-gray-500 px-2 py-6 text-center">
                  No chapter list is available for this title. Use the outer skip buttons when there are
                  multiple audio files.
                </p>
              )}

              {hasAbsChapters && (
                <div className="mb-6">
                  <h3 className="text-xs font-semibold text-gray-500 uppercase tracking-wider px-2 mb-2">
                    Chapters (Audiobookshelf)
                  </h3>
                  <ul className="space-y-1">
                    {nowPlaying.absChapters!.map((ch, i) => {
                      const active = i === activeChapterIdx;
                      return (
                        <li key={`${ch.id}-${ch.start}`}>
                          <button
                            type="button"
                            ref={active ? activeRowRef : undefined}
                            onClick={() => {
                              seek(ch.start);
                              setChaptersMenuOpen(false);
                            }}
                            className={`w-full text-left flex items-start gap-3 px-3 py-2.5 rounded-lg transition-colors ${
                              active
                                ? "bg-brand-600/25 text-brand-200 ring-1 ring-brand-500/40"
                                : "text-gray-300 hover:bg-gray-800 hover:text-white"
                            }`}
                          >
                            <span className="text-xs tabular-nums text-gray-500 shrink-0 pt-0.5">
                              {formatTime(ch.start)}
                            </span>
                            <span className="text-sm flex-1 leading-snug">{ch.title}</span>
                          </button>
                        </li>
                      );
                    })}
                  </ul>
                </div>
              )}

              {nowPlaying.tracks.length > 1 && (
                <div>
                  <h3 className="text-xs font-semibold text-gray-500 uppercase tracking-wider px-2 mb-2">
                    Audio files
                  </h3>
                  <ul className="space-y-1">
                    {nowPlaying.tracks.map((track, i) => {
                      const active = i === currentTrackIndex;
                      return (
                        <li key={track.index}>
                          <button
                            type="button"
                            ref={
                              active && !hasAbsChapters ? activeRowRef : undefined
                            }
                            onClick={() => {
                              jumpToTrack(i);
                              setChaptersMenuOpen(false);
                            }}
                            className={`w-full text-left flex items-center gap-3 px-3 py-2.5 rounded-lg transition-colors ${
                              active
                                ? "bg-brand-600/20 text-brand-300"
                                : "text-gray-400 hover:bg-gray-800 hover:text-gray-200"
                            }`}
                          >
                            <span className="text-xs tabular-nums w-6 text-right shrink-0">
                              {i + 1}
                            </span>
                            <span className="text-sm flex-1 truncate">{track.title}</span>
                            <span className="text-xs tabular-nums text-gray-600 shrink-0">
                              {track.duration > 0 ? formatTime(track.duration) : "—:——"}
                            </span>
                          </button>
                        </li>
                      );
                    })}
                  </ul>
                </div>
              )}
            </div>
          </aside>
        </>
      )}
    </div>
  );
}
