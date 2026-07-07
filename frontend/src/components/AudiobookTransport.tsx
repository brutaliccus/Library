import {
  RotateCcw,
  RotateCw,
  ChevronLeft,
  ChevronRight,
  Play,
  Pause,
  Loader2,
} from "lucide-react";

/** Circular rewind / fast-forward arrows (Lucide SkipBack/SkipForward are bar+triangle “chapter” icons). */
function SkipWithSeconds(props: {
  direction: "back" | "forward";
  seconds: number;
  onClick: () => void;
  title: string;
  variant: "full" | "mini";
}) {
  const { direction, seconds, onClick, title, variant } = props;
  const Icon = direction === "back" ? RotateCcw : RotateCw;
  const iconSz = variant === "full" ? 24 : 16;
  const pad = variant === "full" ? "p-3" : "p-2";
  const labelCls =
    variant === "full"
      ? "text-[11px] font-bold pt-0.5"
      : "text-[9px] font-bold pt-0.5";

  return (
    <button
      type="button"
      onClick={onClick}
      title={title}
      className={`relative ${pad} text-gray-400 hover:text-white transition-colors`}
    >
      <Icon size={iconSz} className="block" strokeWidth={2} aria-hidden />
      <span
        className={`pointer-events-none absolute inset-0 flex items-center justify-center tabular-nums text-current ${labelCls}`}
      >
        {seconds}
      </span>
    </button>
  );
}

function ChapterPrevGlyph(props: { variant: "full" | "mini" }) {
  const h = props.variant === "full" ? "h-[18px]" : "h-[14px]";
  const w = props.variant === "full" ? "w-[3px]" : "w-[2.5px]";
  const chev = props.variant === "full" ? 26 : 18;
  return (
    <span className="flex items-center justify-center -space-x-0.5" aria-hidden>
      <span className={`shrink-0 rounded-[1px] bg-current ${w} ${h}`} />
      <ChevronLeft size={chev} strokeWidth={2} className="shrink-0 opacity-95" />
    </span>
  );
}

function ChapterNextGlyph(props: { variant: "full" | "mini" }) {
  const h = props.variant === "full" ? "h-[18px]" : "h-[14px]";
  const w = props.variant === "full" ? "w-[3px]" : "w-[2.5px]";
  const chev = props.variant === "full" ? 26 : 18;
  return (
    <span className="flex items-center justify-center -space-x-0.5" aria-hidden>
      <ChevronRight size={chev} strokeWidth={2} className="shrink-0 opacity-95" />
      <span className={`shrink-0 rounded-[1px] bg-current ${w} ${h}`} />
    </span>
  );
}

export default function AudiobookTransport(props: {
  variant: "full" | "mini";
  skipSeconds: number;
  seekRelative: (delta: number) => void;
  togglePlay: () => void;
  isPlaying: boolean;
  buffering: boolean;
  skipChapterPrev: () => void;
  skipChapterNext: () => void;
  canPrevChapter: boolean;
  canNextChapter: boolean;
}) {
  const {
    variant,
    skipSeconds,
    seekRelative,
    togglePlay,
    isPlaying,
    buffering,
    skipChapterPrev,
    skipChapterNext,
    canPrevChapter,
    canNextChapter,
  } = props;

  const playSz = variant === "full" ? 28 : 18;
  const playPad = variant === "full" ? "p-4" : "p-2";
  const gap = variant === "full" ? "gap-2 sm:gap-3" : "gap-0.5 sm:gap-1";
  const chPad = variant === "full" ? "p-3" : "p-1.5";

  return (
    <div className={`flex items-center justify-center ${gap} flex-wrap`}>
      <button
        type="button"
        onClick={skipChapterPrev}
        disabled={!canPrevChapter}
        title="Previous chapter"
        className={`${chPad} text-gray-400 hover:text-white transition-colors disabled:opacity-30 disabled:pointer-events-none flex items-center justify-center`}
      >
        <ChapterPrevGlyph variant={variant} />
      </button>

      <SkipWithSeconds
        direction="back"
        seconds={skipSeconds}
        onClick={() => seekRelative(-skipSeconds)}
        title={`Back ${skipSeconds} seconds`}
        variant={variant}
      />

      <button
        type="button"
        onClick={togglePlay}
        className={`${playPad} bg-brand-600 text-white rounded-full hover:bg-brand-500 transition-colors shadow-lg`}
      >
        {buffering ? (
          <Loader2 size={playSz} className="animate-spin" />
        ) : isPlaying ? (
          <Pause size={playSz} />
        ) : (
          <Play size={playSz} className={variant === "full" ? "ml-1" : "ml-0.5"} />
        )}
      </button>

      <SkipWithSeconds
        direction="forward"
        seconds={skipSeconds}
        onClick={() => seekRelative(skipSeconds)}
        title={`Forward ${skipSeconds} seconds`}
        variant={variant}
      />

      <button
        type="button"
        onClick={skipChapterNext}
        disabled={!canNextChapter}
        title="Next chapter"
        className={`${chPad} text-gray-400 hover:text-white transition-colors disabled:opacity-30 disabled:pointer-events-none flex items-center justify-center`}
      >
        <ChapterNextGlyph variant={variant} />
      </button>
    </div>
  );
}
