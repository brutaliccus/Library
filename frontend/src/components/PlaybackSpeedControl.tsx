import { useEffect, useRef, useState } from "react";
import { Gauge, Minus, Plus } from "lucide-react";
import {
  formatPlaybackSpeed,
  sliderToSpeed,
  speedToSlider,
  stepPlaybackSpeed,
} from "../utils/playbackSpeed";

interface Props {
  rate: number;
  onChange: (rate: number) => void;
  /** Compact icon-only trigger for the mini player. */
  compact?: boolean;
}

/**
 * Speed icon that opens a volume-style bar above the player.
 * 0.5× far left, 1.0× middle, 3.0× far right; ± buttons step discrete increments.
 */
export default function PlaybackSpeedControl({ rate, onChange, compact }: Props) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => {
      if (!rootRef.current?.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onDoc);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDoc);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const slider = speedToSlider(rate);

  const panel = (
    <div
      className={
        compact
          ? `absolute bottom-[calc(100%+0.5rem)] left-1/2 -translate-x-1/2 z-[60]
            w-[min(18rem,calc(100vw-2rem))] px-3 py-2.5 rounded-xl
            bg-gray-900/95 backdrop-blur-md border border-gray-700/80
            shadow-xl shadow-black/50`
          : `fixed inset-x-3 z-[110] bottom-[calc(env(safe-area-inset-bottom,0px)+6.5rem)]
            max-w-lg mx-auto px-4 py-3 rounded-xl
            bg-gray-900/95 backdrop-blur-md border border-gray-700/80
            shadow-xl shadow-black/50`
      }
      role="dialog"
      aria-label="Adjust playback speed"
    >
      <div className="flex items-center justify-between mb-2 px-0.5">
        <span className="text-[10px] uppercase tracking-wide text-gray-500">Speed</span>
        <span className="text-sm font-semibold tabular-nums text-brand-300">
          {formatPlaybackSpeed(rate)}
        </span>
      </div>
      <div className="flex items-center gap-2">
        <button
          type="button"
          onClick={() => onChange(stepPlaybackSpeed(rate, -1))}
          className="p-1.5 rounded-lg text-gray-400 hover:text-white hover:bg-gray-800 transition-colors"
          aria-label="Slower"
          title="Slower"
        >
          <Minus size={16} />
        </button>
        <input
          type="range"
          min={0}
          max={1}
          step={0.001}
          value={slider}
          onChange={(e) => onChange(sliderToSpeed(parseFloat(e.target.value)))}
          className="flex-1 accent-brand-500 h-1.5 cursor-pointer"
          aria-valuemin={0.5}
          aria-valuemax={3}
          aria-valuenow={rate}
          aria-label="Playback speed slider"
        />
        <button
          type="button"
          onClick={() => onChange(stepPlaybackSpeed(rate, 1))}
          className="p-1.5 rounded-lg text-gray-400 hover:text-white hover:bg-gray-800 transition-colors"
          aria-label="Faster"
          title="Faster"
        >
          <Plus size={16} />
        </button>
      </div>
      <div className="flex justify-between mt-1 px-1 text-[10px] text-gray-600 tabular-nums">
        <span>0.5×</span>
        <span>1.0×</span>
        <span>3.0×</span>
      </div>
    </div>
  );

  return (
    <div ref={rootRef} className="relative shrink-0">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className={`inline-flex items-center justify-center gap-1 rounded-lg transition-colors ${
          open
            ? "text-brand-400 bg-gray-800"
            : "text-gray-400 hover:text-white hover:bg-gray-800/80"
        } ${compact ? "p-2" : "px-2 py-1.5"}`}
        title={`Speed ${formatPlaybackSpeed(rate)}`}
        aria-label={`Playback speed ${formatPlaybackSpeed(rate)}`}
        aria-expanded={open}
      >
        <Gauge size={compact ? 16 : 18} />
        {!compact && (
          <span className="text-xs tabular-nums font-medium">{formatPlaybackSpeed(rate)}</span>
        )}
      </button>

      {open && panel}
    </div>
  );
}
