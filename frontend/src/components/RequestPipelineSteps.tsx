/** Multi-step progress for LibraForge download pipeline. */

export interface PipelineStepProps {
  status: string;
  detail?: string | null;
  progress_percent?: number | null;
  progress_bytes?: number | null;
  progress_total_bytes?: number | null;
  progress_speed_bps?: number | null;
}

const STEPS = [
  { id: "download", label: "Download" },
  { id: "metadata", label: "Metadata" },
  { id: "m4b", label: "M4B" },
  { id: "folder", label: "Folder Forge" },
  { id: "finalize", label: "Finalize" },
] as const;

type StepId = (typeof STEPS)[number]["id"];

const DOWNLOAD_STATUSES = new Set([
  "pending",
  "sent_to_rd",
  "downloading_rd",
  "transferring",
]);

const ACTIVE = new Set([
  ...DOWNLOAD_STATUSES,
  "organizing",
  "metadata_forge",
  "m4b_convert",
  "folder_forge",
  "finalizing",
  "quarantined",
]);

function currentStepId(status: string): StepId | "done" | "failed" {
  if (status === "completed") return "done";
  if (status === "failed" || status === "cancelled" || status === "admin_rejected") {
    return "failed";
  }
  if (DOWNLOAD_STATUSES.has(status)) return "download";
  if (status === "metadata_forge" || status === "organizing" || status === "quarantined") {
    return "metadata";
  }
  if (status === "m4b_convert") return "m4b";
  if (status === "folder_forge") return "folder";
  if (status === "finalizing") return "finalize";
  return "download";
}

function stepIndex(id: StepId | "done" | "failed"): number {
  if (id === "done") return STEPS.length;
  if (id === "failed") return -1;
  return STEPS.findIndex((s) => s.id === id);
}

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

export default function RequestPipelineSteps({
  status,
  detail,
  progress_percent,
  progress_bytes,
  progress_total_bytes,
  progress_speed_bps,
}: PipelineStepProps) {
  if (!ACTIVE.has(status) && status !== "completed") {
    return null;
  }

  const cur = currentStepId(status);
  const curIdx = stepIndex(cur);
  const quarantined = status === "quarantined";
  const hasPercent = progress_percent != null && progress_percent >= 0;
  const showBar =
    hasPercent ||
    status === "downloading_rd" ||
    status === "transferring" ||
    status === "metadata_forge" ||
    status === "m4b_convert" ||
    status === "folder_forge" ||
    status === "finalizing";

  const speed = formatSpeed(progress_speed_bps);
  const byteLabel =
    progress_bytes != null && progress_total_bytes != null && progress_total_bytes > 0
      ? `${formatBytes(progress_bytes)} / ${formatBytes(progress_total_bytes)}`
      : progress_bytes != null && progress_bytes > 0
        ? formatBytes(progress_bytes)
        : null;

  return (
    <div className="mt-3 space-y-2">
      <div className="flex items-center gap-1">
        {STEPS.map((step, i) => {
          const done = cur === "done" || (curIdx >= 0 && i < curIdx);
          const active = curIdx === i;
          const warn = quarantined && step.id === "metadata";
          return (
            <div key={step.id} className="flex-1 min-w-0 flex flex-col items-center gap-1">
              <div
                className={`h-1.5 w-full rounded-full transition-colors ${
                  warn
                    ? "bg-amber-500"
                    : done
                      ? "bg-emerald-500"
                      : active
                        ? "bg-sky-500"
                        : "bg-gray-700"
                }`}
              />
              <span
                className={`text-[10px] truncate max-w-full ${
                  warn
                    ? "text-amber-400"
                    : done
                      ? "text-emerald-400/80"
                      : active
                        ? "text-sky-300"
                        : "text-gray-600"
                }`}
              >
                {step.label}
              </span>
            </div>
          );
        })}
      </div>

      {showBar && status !== "completed" && (
        <div className="h-2 bg-gray-700/80 rounded-full overflow-hidden">
          {hasPercent ? (
            <div
              className={`h-full transition-[width] duration-300 ease-out ${
                quarantined
                  ? "bg-amber-500"
                  : "bg-gradient-to-r from-sky-500 to-teal-400"
              }`}
              style={{ width: `${Math.min(100, Math.max(0, progress_percent ?? 0))}%` }}
            />
          ) : (
            <div
              className={`h-full w-1/3 rounded-full animate-pulse ${
                quarantined ? "bg-amber-500/80" : "bg-sky-500/80"
              }`}
            />
          )}
        </div>
      )}

      {(detail || showBar) && status !== "completed" && (
        <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-0.5 text-xs text-gray-400">
          <span className="truncate">{detail || "Working…"}</span>
          <span className="shrink-0 tabular-nums text-gray-500">
            {hasPercent && `${Math.round(progress_percent!)}%`}
            {hasPercent && (speed || byteLabel) && " · "}
            {speed}
            {speed && byteLabel && " · "}
            {byteLabel}
          </span>
        </div>
      )}
    </div>
  );
}
