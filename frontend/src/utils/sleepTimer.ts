/** Sleep timer presets: Off -> 5..30 by 5, then 60, then Off. */
export const SLEEP_TIMER_MINUTES = [5, 10, 15, 20, 25, 30, 60] as const;

export type SleepTimerMinutes = (typeof SLEEP_TIMER_MINUTES)[number];

/** Advance one step: null -> 5 -> ... -> 60 -> null. */
export function cycleSleepTimerMinutes(
  current: number | null | undefined
): number | null {
  if (current == null) return SLEEP_TIMER_MINUTES[0];
  const idx = SLEEP_TIMER_MINUTES.indexOf(current as SleepTimerMinutes);
  if (idx < 0) return SLEEP_TIMER_MINUTES[0];
  if (idx >= SLEEP_TIMER_MINUTES.length - 1) return null;
  return SLEEP_TIMER_MINUTES[idx + 1];
}

export function formatSleepCountdown(sec: number): string {
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = sec % 60;
  const pad = (n: number) => n.toString().padStart(2, "0");
  if (h > 0) return `${h}:${pad(m)}:${pad(s)}`;
  return `${m}:${pad(s)}`;
}
