import { useCallback, useRef, useState } from "react";

interface Props {
  /** 0–100 */
  progress: number;
  disabled?: boolean;
  /** Called with scrub fraction 0–1 while dragging and on release */
  onSeekFraction: (fraction: number) => void;
  className?: string;
  barClassName?: string;
  showThumb?: boolean;
}

export default function PlaybackScrubber({
  progress,
  disabled = false,
  onSeekFraction,
  className = "h-2",
  barClassName = "bg-gray-800",
  showThumb = true,
}: Props) {
  const barRef = useRef<HTMLDivElement>(null);
  const dragging = useRef(false);
  const [dragPct, setDragPct] = useState<number | null>(null);

  const fractionFromClientX = useCallback((clientX: number) => {
    const bar = barRef.current;
    if (!bar) return 0;
    const rect = bar.getBoundingClientRect();
    if (rect.width <= 0) return 0;
    return Math.max(0, Math.min(1, (clientX - rect.left) / rect.width));
  }, []);

  const finishDrag = useCallback(
    (clientX: number) => {
      dragging.current = false;
      const f = fractionFromClientX(clientX);
      setDragPct(null);
      onSeekFraction(f);
    },
    [fractionFromClientX, onSeekFraction]
  );

  const onPointerDown = (e: React.PointerEvent<HTMLDivElement>) => {
    if (disabled) return;
    dragging.current = true;
    e.currentTarget.setPointerCapture(e.pointerId);
    const f = fractionFromClientX(e.clientX);
    setDragPct(f * 100);
  };

  const onPointerMove = (e: React.PointerEvent<HTMLDivElement>) => {
    if (!dragging.current || disabled) return;
    setDragPct(fractionFromClientX(e.clientX) * 100);
  };

  const onPointerUp = (e: React.PointerEvent<HTMLDivElement>) => {
    if (!dragging.current) return;
    try {
      e.currentTarget.releasePointerCapture(e.pointerId);
    } catch {
      /* already released */
    }
    finishDrag(e.clientX);
  };

  const onPointerCancel = (e: React.PointerEvent<HTMLDivElement>) => {
    if (!dragging.current) return;
    finishDrag(e.clientX);
  };

  const displayPct = dragPct ?? progress;

  return (
    <div
      ref={barRef}
      className={`${className} ${barClassName} rounded-full cursor-pointer group touch-none select-none ${
        disabled ? "opacity-50 pointer-events-none" : ""
      }`}
      role="slider"
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={Math.round(displayPct)}
      aria-disabled={disabled}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerUp}
      onPointerCancel={onPointerCancel}
    >
      <div
        className="h-full bg-brand-500 rounded-full relative transition-[width] duration-75 group-hover:bg-brand-400"
        style={{ width: `${displayPct}%` }}
      >
        {showThumb && (
          <div
            className={`absolute right-0 top-1/2 -translate-y-1/2 w-3.5 h-3.5 bg-white rounded-full shadow transition-opacity ${
              dragPct != null ? "opacity-100 scale-110" : "opacity-0 group-hover:opacity-100"
            }`}
          />
        )}
      </div>
    </div>
  );
}
