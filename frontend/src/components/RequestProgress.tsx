export interface RequestProgressData {
  status: string;
  detail?: string | null;
  progress_percent?: number | null;
  progress_bytes?: number | null;
  progress_total_bytes?: number | null;
  progress_speed_bps?: number | null;
}

const ACTIVE_STATUSES = new Set([
  "downloading_rd",
  "transferring",
  "organizing",
  "sent_to_rd",
  "pending",
]);

function formatBytes(bytes: number): string {
  if (bytes >= 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024 * 1024)).toFixed(1)} GB`;
  if (bytes >= 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  if (bytes >= 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${bytes} B`;
}

function formatSpeed(bps: number | null | undefined): string | null {
  if (!bps || bps <= 0) return null;
  const mbps = bps / (1024 * 1024);
  if (mbps >= 1) return `${mbps.toFixed(1)} MB/s`;
  return `${(bps / 1024).toFixed(0)} KB/s`;
}

export default function RequestProgress({
  status,
  detail,
  progress_percent,
  progress_bytes,
  progress_total_bytes,
  progress_speed_bps,
}: RequestProgressData) {
  if (!ACTIVE_STATUSES.has(status) || status === "completed" || status === "failed") {
    return null;
  }

  const speed = formatSpeed(progress_speed_bps);
  const hasPercent = progress_percent != null && progress_percent >= 0;
  const showBar = hasPercent || status === "downloading_rd" || status === "transferring";

  if (!showBar && !detail) return null;

  const byteLabel =
    progress_bytes != null && progress_total_bytes != null && progress_total_bytes > 0
      ? `${formatBytes(progress_bytes)} / ${formatBytes(progress_total_bytes)}`
      : progress_bytes != null && progress_bytes > 0
        ? formatBytes(progress_bytes)
        : null;

  return (
    <div className="mt-3 space-y-1.5">
      {showBar && (
        <div className="h-2 bg-gray-700/80 rounded-full overflow-hidden">
          {hasPercent ? (
            <div
              className="h-full bg-gradient-to-r from-purple-500 to-indigo-400 transition-[width] duration-300 ease-out"
              style={{ width: `${Math.min(100, Math.max(0, progress_percent))}%` }}
            />
          ) : (
            <div className="h-full w-1/3 bg-purple-500/80 rounded-full animate-pulse" />
          )}
        </div>
      )}
      <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-0.5 text-xs text-gray-400">
        <span className="truncate">{detail || "Working…"}</span>
        <span className="shrink-0 tabular-nums text-gray-500">
          {hasPercent && `${Math.round(progress_percent)}%`}
          {hasPercent && (speed || byteLabel) && " · "}
          {speed}
          {speed && byteLabel && " · "}
          {byteLabel}
        </span>
      </div>
    </div>
  );
}
