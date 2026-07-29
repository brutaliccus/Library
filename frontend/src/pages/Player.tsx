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
import PlaybackSpeedControl from "../components/PlaybackSpeedControl";
import SleepTimerControl from "../components/SleepTimerControl";
import CoverImage from "../components/CoverImage";
import {
  chapterNavAvailability,
  currentChapterLabel,
  indexOfChapterAtTime,
  playbackScope,
  seekTimeFromScope,
} from "../utils/playerNav";

const SKIP_SECONDS = 15;

function formatTime(s: number): string {
  if (!s || !isFinite(s)) return "0:00:00";
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = Math.floor(s % 60);
  const pad = (n: number) => n.toString().padStart(2, "0");
  return `${h}:${pad(m)}:${pad(sec)}`;
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
    upNext,
    removeFromUpNext,
    clearUpNext,
  } = usePlayer();

  const [chaptersMenuOpen, setChaptersMenuOpen] = useState(false);
  const activeRowRef = useRef<HTMLButtonElement | null>(null);

  useEffect(() => {
    if (!nowPlaying) {
      navigate("/my-library", { replace: true });
    }
  }, [nowPlaying, navigate]);

  const hasAbsChapters = (nowPlaying?.absChapters?.length ?? 0) > 0;
  const activeChapterIdx =
    nowPlaying && hasAbsChapters
      ? indexOfChapterAtTime(nowPlaying.absChapters!, currentTime)
      : -1;

  // Scroll to active chapter only when the drawer opens or the chapter/track
  // boundary changes — not on every progress tick (that fought user scroll).
  useEffect(() => {
    if (chaptersMenuOpen && activeRowRef.current) {
      activeRowRef.current.scrollIntoView({ block: "nearest", behavior: "smooth" });
    }
  }, [chaptersMenuOpen, activeChapterIdx, currentTrackIndex, nowPlaying?.absChapters?.length]);

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
  const showTrackLine = nowPlaying.tracks.length > 1;
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
            <PlaybackSpeedControl rate={playbackRate} onChange={setPlaybackRate} />
            <SleepTimerControl
              minutes={sleepTimerPresetMinutes}
              secondsRemaining={sleepTimerSecondsRemaining}
              onChange={setSleepTimer}
            />

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
            className="fixed inset-y-0 left-0 z-[120] w-full max-w-md bg-gray-950 border-r border-gray-800 shadow-2xl flex flex-col pt-[env(safe-area-inset-top,0px)] pb-[env(safe-area-inset-bottom,0px)] pl-[env(safe-area-inset-left,0px)]"
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

            <div className="flex-1 overflow-y-auto px-3 py-3">
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

              <div className="mt-6 border-t border-gray-800 pt-4">
                <div className="flex items-center justify-between px-2 mb-2">
                  <h3 className="text-xs font-semibold text-gray-500 uppercase tracking-wider">
                    Up Next ({upNext.length})
                  </h3>
                  {upNext.length > 0 && (
                    <button
                      type="button"
                      onClick={() => clearUpNext()}
                      className="text-[11px] text-gray-500 hover:text-gray-300"
                    >
                      Clear
                    </button>
                  )}
                </div>
                {upNext.length === 0 ? (
                  <p className="text-sm text-gray-600 px-2 py-2">
                    Add titles from library details to play them after this book.
                  </p>
                ) : (
                  <ul className="space-y-1">
                    {upNext.map((item, i) => (
                      <li
                        key={`${item.source}-${item.id}-${i}`}
                        className="flex items-center gap-2 px-2 py-2 rounded-lg hover:bg-gray-800/80"
                      >
                        <CoverImage
                          src={item.coverUrl}
                          alt=""
                          className="w-8 h-12 rounded object-cover shrink-0"
                          fallback={<div className="w-8 h-12 rounded bg-gray-800 shrink-0" />}
                        />
                        <div className="min-w-0 flex-1">
                          <p className="text-sm text-gray-200 truncate">{item.title}</p>
                          {item.author && (
                            <p className="text-[11px] text-gray-500 truncate">{item.author}</p>
                          )}
                        </div>
                        <button
                          type="button"
                          onClick={() => removeFromUpNext(i)}
                          className="p-1.5 text-gray-500 hover:text-red-300"
                          aria-label="Remove from Up Next"
                        >
                          <X size={14} />
                        </button>
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            </div>
          </aside>
        </>
      )}
    </div>
  );
}
