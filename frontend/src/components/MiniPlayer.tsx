import { usePlayer } from "../contexts/PlayerContext";
import { X, Maximize2, Volume2 } from "lucide-react";
import AudiobookTransport from "./AudiobookTransport";
import PlaybackScrubber from "./PlaybackScrubber";
import CoverImage from "./CoverImage";
import { chapterNavAvailability, playbackScope, seekTimeFromScope } from "../utils/playerNav";

const SLEEP_TIMER_MINUTES = [5, 10, 15, 20, 25, 30, 60] as const;
const SKIP_SECONDS = 15;

function formatTime(s: number): string {
  if (!s || !isFinite(s)) return "0:00";
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = Math.floor(s % 60);
  const pad = (n: number) => n.toString().padStart(2, "0");
  return h > 0 ? `${h}:${pad(m)}:${pad(sec)}` : `${m}:${pad(sec)}`;
}

function formatCountdown(sec: number): string {
  const m = Math.floor(sec / 60);
  const s = sec % 60;
  return `${m}:${s.toString().padStart(2, "0")}`;
}

export default function MiniPlayer() {
  const {
    nowPlaying,
    isPlaying,
    currentTime,
    buffering,
    volume,
    currentTrackIndex,
    togglePlay,
    seekRelative,
    seek,
    setVolume,
    setExpanded,
    dismissPlayer,
    sleepTimerPresetMinutes,
    sleepTimerSecondsRemaining,
    setSleepTimer,
    skipChapterPrev,
    skipChapterNext,
  } = usePlayer();

  if (!nowPlaying) return null;

  const scope = playbackScope(nowPlaying, currentTime, currentTrackIndex);
  const scopeProgress =
    scope.duration > 0 ? (scope.position / scope.duration) * 100 : 0;
  const { prev: canPrevChapter, next: canNextChapter } = chapterNavAvailability(
    nowPlaying,
    currentTime,
    currentTrackIndex
  );

  const handleScrubFraction = (fraction: number) => {
    seek(seekTimeFromScope(scope, fraction));
  };

  return (
    <div className="fixed bottom-0 left-0 right-0 z-50 bg-gray-900 border-t border-gray-800 shadow-2xl shadow-black/60 pb-[env(safe-area-inset-bottom,0px)]">
      <PlaybackScrubber
        progress={scopeProgress}
        disabled={scope.duration <= 0}
        onSeekFraction={handleScrubFraction}
        className="h-1"
        barClassName="bg-gray-800"
        showThumb={false}
      />

      <div className="max-w-6xl mx-auto px-4 flex items-center gap-4 h-16">
        <CoverImage
          src={nowPlaying.coverUrl}
          alt=""
          className="w-10 h-10 rounded object-cover shrink-0"
          fallback={<div className="w-10 h-10 rounded bg-gray-800 shrink-0" />}
        />

        <div className="flex-1 min-w-0">
          <p className="text-sm text-gray-100 font-medium truncate">
            {nowPlaying.title}
          </p>
          {nowPlaying.author && (
            <p className="text-xs text-gray-500 truncate">{nowPlaying.author}</p>
          )}
        </div>

        <span className="text-xs text-gray-500 tabular-nums hidden sm:block">
          {formatTime(scope.position)} / {formatTime(scope.duration)}
        </span>

        <AudiobookTransport
          variant="mini"
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

        <div className="flex items-center gap-1 shrink-0">
          <select
            value={sleepTimerPresetMinutes ?? ""}
            onChange={(e) => {
              const v = e.target.value;
              if (v === "") setSleepTimer(null);
              else setSleepTimer(Number(v));
            }}
            className="text-[11px] bg-gray-800 border border-gray-700 text-gray-200 rounded px-1.5 py-1 max-w-[4.75rem] sm:max-w-[6rem] focus:outline-none focus:ring-1 focus:ring-brand-500"
            title={
              sleepTimerSecondsRemaining != null && sleepTimerSecondsRemaining > 0
                ? `Pauses in ${formatCountdown(sleepTimerSecondsRemaining)}`
                : "Sleep timer"
            }
            aria-label="Sleep timer"
          >
            <option value="">Sleep</option>
            {SLEEP_TIMER_MINUTES.map((m) => (
              <option key={m} value={m}>
                {m}m
              </option>
            ))}
          </select>
        </div>

        <div className="hidden md:flex items-center gap-2 w-28">
          <Volume2 size={14} className="text-gray-500 shrink-0" />
          <input
            type="range"
            min={0}
            max={1}
            step={0.05}
            value={volume}
            onChange={(e) => setVolume(parseFloat(e.target.value))}
            className="w-full accent-brand-500 h-1"
          />
        </div>

        <button
          onClick={() => setExpanded(true)}
          className="p-2 text-gray-400 hover:text-white transition-colors"
          title="Expand player"
        >
          <Maximize2 size={16} />
        </button>

        <button
          onClick={dismissPlayer}
          className="p-2 text-gray-400 hover:text-white transition-colors"
          title="Close player"
          aria-label="Close player"
        >
          <X size={16} />
        </button>
      </div>
    </div>
  );
}
