import { RefreshCw, X } from "lucide-react";
import { useServiceWorkerUpdate } from "../hooks/useServiceWorkerUpdate";

export default function UpdateBanner() {
  const { showBanner, applyUpdate, dismissUpdate } = useServiceWorkerUpdate();

  if (!showBanner) return null;

  return (
    <div
      role="status"
      aria-live="polite"
      className="fixed top-0 left-0 right-0 z-[110] px-3 pt-[max(0.75rem,env(safe-area-inset-top))] pb-3 pointer-events-none"
    >
      <div className="mx-auto max-w-lg pointer-events-auto flex items-center gap-3 rounded-lg border border-indigo-500/40 bg-indigo-950/95 px-4 py-3 shadow-lg backdrop-blur-sm">
        <RefreshCw size={18} className="shrink-0 text-indigo-300" aria-hidden />
        <p className="flex-1 text-sm text-indigo-50">
          <span className="font-medium">Update available!</span>
          <span className="text-indigo-200/90"> A new version of the app is ready.</span>
        </p>
        <button
          type="button"
          onClick={applyUpdate}
          className="shrink-0 rounded-md bg-indigo-500 px-3 py-1.5 text-sm font-medium text-white hover:bg-indigo-400 transition-colors"
        >
          Update
        </button>
        <button
          type="button"
          onClick={dismissUpdate}
          className="shrink-0 rounded p-1 text-indigo-300 hover:bg-indigo-800/60 hover:text-indigo-100 transition-colors"
          aria-label="Dismiss update notice"
        >
          <X size={18} />
        </button>
      </div>
    </div>
  );
}
