import { useToast } from "../contexts/ToastContext";
import { CheckCircle2, XCircle, Info, X } from "lucide-react";

const icons = {
  success: CheckCircle2,
  error: XCircle,
  info: Info,
};

const styles = {
  success: "bg-green-900/90 text-green-100 border-green-700",
  error: "bg-red-900/90 text-red-100 border-red-700",
  info: "bg-gray-800 text-gray-100 border-gray-600",
};

export default function ToastContainer() {
  const { toasts, removeToast } = useToast();

  if (toasts.length === 0) return null;

  return (
    <div
      className="fixed bottom-4 right-4 z-[100] flex flex-col gap-2 max-w-sm w-full pointer-events-none pb-[env(safe-area-inset-bottom,0px)]"
      aria-live="polite"
    >
      <div className="flex flex-col gap-2 pointer-events-auto">
        {toasts.map((t) => {
          const Icon = icons[t.type];
          return (
            <div
              key={t.id}
              className={`flex items-start gap-2 px-4 py-3 rounded-lg border shadow-lg ${styles[t.type]}`}
            >
              <Icon size={18} className="shrink-0 mt-0.5" />
              <p className="text-sm flex-1">{t.message}</p>
              <button
                type="button"
                onClick={() => removeToast(t.id)}
                className="shrink-0 p-0.5 rounded hover:bg-white/10 transition-colors"
                aria-label="Dismiss"
              >
                <X size={14} />
              </button>
            </div>
          );
        })}
      </div>
    </div>
  );
}
