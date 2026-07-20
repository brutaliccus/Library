import { Download, X } from "lucide-react";
import type { AppUpdateCheckResult } from "../utils/appUpdateCheck";

interface Props {
  update: AppUpdateCheckResult;
  downloading?: boolean;
  onDismiss: () => void;
  onDownload: () => void;
}

export default function AppUpdateBanner({
  update,
  downloading = false,
  onDismiss,
  onDownload,
}: Props) {
  return (
    <div
      role="status"
      aria-live="polite"
      className="fixed top-0 left-0 right-0 z-[110] px-3 pt-[max(0.75rem,env(safe-area-inset-top))] pb-3 pointer-events-none"
    >
      <div className="mx-auto max-w-lg pointer-events-auto flex items-center gap-3 rounded-lg border border-emerald-500/40 bg-emerald-950/95 px-4 py-3 shadow-lg backdrop-blur-sm">
        <Download size={18} className="shrink-0 text-emerald-300" aria-hidden />
        <p className="flex-1 text-sm text-emerald-50 min-w-0">
          <span className="font-medium">App update available</span>
          <span className="text-emerald-200/90">
            {" "}
            Version {update.versionLabel} is ready to install.
          </span>
        </p>
        <button
          type="button"
          onClick={onDownload}
          disabled={downloading}
          className="shrink-0 rounded-md bg-emerald-500 px-3 py-1.5 text-sm font-medium text-white hover:bg-emerald-400 transition-colors disabled:opacity-60"
        >
          {downloading ? "Downloading…" : "Update"}
        </button>
        <button
          type="button"
          onClick={onDismiss}
          className="shrink-0 rounded p-1 text-emerald-300 hover:bg-emerald-800/60 hover:text-emerald-100 transition-colors"
          aria-label="Dismiss update notice"
        >
          <X size={18} />
        </button>
      </div>
    </div>
  );
}
