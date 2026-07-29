/** Discrete audiobook playback speeds (0.5 left -> 1.0 middle -> 3.0 right). */
export const PLAYBACK_SPEED_STEPS = [
  0.5, 0.6, 0.7, 0.8, 0.9, 1.0, 1.05, 1.1, 1.15, 1.25, 1.5, 2.0, 2.5, 3.0,
] as const;

export type PlaybackSpeed = (typeof PLAYBACK_SPEED_STEPS)[number];

const ONE_IDX = PLAYBACK_SPEED_STEPS.indexOf(1.0);

export function formatPlaybackSpeed(rate: number): string {
  const n = snapPlaybackSpeed(rate);
  if (Number.isInteger(n) || Math.abs(n - Math.round(n)) < 0.001) {
    return `${n.toFixed(1)}x`;
  }
  const s = n.toFixed(2).replace(/0$/, "");
  return `${s}x`;
}

export function snapPlaybackSpeed(rate: number): number {
  if (!isFinite(rate) || rate <= 0) return 1;
  let best: number = PLAYBACK_SPEED_STEPS[0];
  let bestDist = Math.abs(rate - best);
  for (const step of PLAYBACK_SPEED_STEPS) {
    const d = Math.abs(rate - step);
    if (d < bestDist) {
      best = step;
      bestDist = d;
    }
  }
  return best;
}

export function playbackSpeedIndex(rate: number): number {
  const snapped = snapPlaybackSpeed(rate);
  const idx = PLAYBACK_SPEED_STEPS.indexOf(snapped as PlaybackSpeed);
  return idx >= 0 ? idx : ONE_IDX;
}

export function stepPlaybackSpeed(rate: number, delta: 1 | -1): number {
  const idx = playbackSpeedIndex(rate);
  const next = Math.max(0, Math.min(PLAYBACK_SPEED_STEPS.length - 1, idx + delta));
  return PLAYBACK_SPEED_STEPS[next];
}

/** Map a rate onto a 0-1 slider where 1.0x sits at the visual midpoint. */
export function speedToSlider(rate: number): number {
  const idx = playbackSpeedIndex(rate);
  if (idx <= ONE_IDX) {
    return ONE_IDX === 0 ? 0 : (idx / ONE_IDX) * 0.5;
  }
  const right = PLAYBACK_SPEED_STEPS.length - 1 - ONE_IDX;
  return 0.5 + ((idx - ONE_IDX) / right) * 0.5;
}

export function sliderToSpeed(t: number): number {
  const x = Math.max(0, Math.min(1, t));
  if (x <= 0.5) {
    const f = ONE_IDX === 0 ? 0 : (x / 0.5) * ONE_IDX;
    return PLAYBACK_SPEED_STEPS[Math.round(f)];
  }
  const right = PLAYBACK_SPEED_STEPS.length - 1 - ONE_IDX;
  const f = ONE_IDX + ((x - 0.5) / 0.5) * right;
  return PLAYBACK_SPEED_STEPS[Math.round(f)];
}
