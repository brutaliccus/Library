import { cycleSleepTimerMinutes, formatSleepCountdown } from "../utils/sleepTimer";

interface Props {
  /** Active preset minutes, or null when off. */
  minutes: number | null;
  /** Seconds remaining (for tooltip / label). */
  secondsRemaining?: number | null;
  onChange: (minutes: number | null) => void;
  /** Compact icon for mini player. */
  compact?: boolean;
}

/** Style B sleep-timer dial: ticks + centered minutes + large exponent Z. Glow uses brand theme tokens. */
function SleepTimerIcon({ minutes, size }: { minutes: number | null; size: number }) {
  const active = minutes != null;
  const label = active ? String(minutes) : "–";
  const fontSize = minutes != null && minutes >= 10 ? 11 : 13;
  // Tick marks around the dial
  const ticks = Array.from({ length: 12 }, (_, i) => {
    const a = (i / 12) * Math.PI * 2 - Math.PI / 2;
    const x1 = 18 + Math.cos(a) * 10.2;
    const y1 = 22 + Math.sin(a) * 10.2;
    const x2 = 18 + Math.cos(a) * 11.8;
    const y2 = 22 + Math.sin(a) * 11.8;
    return (
      <line
        key={i}
        x1={x1}
        y1={y1}
        x2={x2}
        y2={y2}
        stroke="currentColor"
        strokeWidth={1.2}
        strokeLinecap="round"
        opacity={0.7}
      />
    );
  });

  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 40 40"
      xmlns="http://www.w3.org/2000/svg"
      aria-hidden
      className="block"
    >
      <circle
        cx="18"
        cy="22"
        r="13"
        fill={active ? "rgb(var(--brand-500) / 0.12)" : "transparent"}
        stroke="currentColor"
        strokeWidth={active ? 1.85 : 1.55}
      />
      {ticks}
      <text
        x="18"
        y="26.4"
        textAnchor="middle"
        fill="currentColor"
        fontSize={fontSize}
        fontWeight={700}
        fontFamily="ui-rounded, Segoe UI, system-ui, sans-serif"
      >
        {label}
      </text>
      {/* Large exponent-style Z (single letter, top-right). */}
      <text
        x="31.5"
        y="14"
        textAnchor="middle"
        fill="currentColor"
        fontSize={active ? 16 : 13}
        fontWeight={800}
        fontFamily="ui-rounded, Segoe UI, system-ui, sans-serif"
        opacity={active ? 0.95 : 0.4}
      >
        Z
      </text>
    </svg>
  );
}

export default function SleepTimerControl({
  minutes,
  secondsRemaining,
  onChange,
  compact,
}: Props) {
  const active = minutes != null;
  const remaining =
    secondsRemaining != null && secondsRemaining > 0
      ? formatSleepCountdown(secondsRemaining)
      : null;
  const title = active
    ? remaining
      ? `Sleep timer ${minutes}m — pauses in ${remaining} (tap to change)`
      : `Sleep timer ${minutes}m (tap to change)`
    : "Sleep timer off (tap to set 5 minutes)";

  return (
    <button
      type="button"
      onClick={() => onChange(cycleSleepTimerMinutes(minutes))}
      className={`inline-flex items-center justify-center rounded-lg transition-all ${
        compact ? "p-1.5" : "p-2"
      } ${
        active
          ? "text-brand-400 bg-brand-500/10 ring-1 ring-brand-500/40 shadow-[0_0_14px_rgb(var(--brand-500)/0.45)]"
          : "text-gray-400 hover:text-white hover:bg-gray-800/80"
      }`}
      title={title}
      aria-label={title}
      aria-pressed={active}
    >
      <SleepTimerIcon minutes={minutes} size={compact ? 22 : 28} />
      {!compact && active && remaining && (
        <span className="ml-1.5 text-xs tabular-nums text-brand-300">{remaining}</span>
      )}
    </button>
  );
}
