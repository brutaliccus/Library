import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { BookOpen, Compass, Download, X } from "lucide-react";

const STORAGE_KEY = "listener-coach-dismissed";

const STEPS = [
  {
    title: "My Library",
    body: "Your books live here — including a short My Collection list for quick access.",
    to: "/my-library",
    icon: BookOpen,
  },
  {
    title: "Browse",
    body: "Find titles in the catalog and get more for your library.",
    to: "/",
    icon: Compass,
  },
  {
    title: "Downloads",
    body: "Track requests here and wait until a title is Ready to listen.",
    to: "/downloads",
    icon: Download,
  },
] as const;

export default function CoachMarks() {
  const [open, setOpen] = useState(false);
  const [step, setStep] = useState(0);

  useEffect(() => {
    try {
      if (!localStorage.getItem(STORAGE_KEY)) setOpen(true);
    } catch {
      setOpen(true);
    }
  }, []);

  const dismiss = () => {
    setOpen(false);
    try {
      localStorage.setItem(STORAGE_KEY, "1");
    } catch {
      /* ignore */
    }
  };

  if (!open) return null;

  const current = STEPS[step];
  const Icon = current.icon;
  const isLast = step >= STEPS.length - 1;

  return (
    <div className="fixed inset-0 z-[200] flex items-center justify-center p-4 pt-[max(1rem,env(safe-area-inset-top,0px))] pb-[max(1rem,env(safe-area-inset-bottom,0px))] px-[max(1rem,env(safe-area-inset-left,0px))] bg-black/75">
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="coach-marks-title"
        className="w-full max-w-md rounded-2xl border border-gray-700 bg-gray-900 shadow-2xl shadow-black/50 p-5 max-h-[min(90dvh,calc(100dvh-env(safe-area-inset-top,0px)-env(safe-area-inset-bottom,0px)-2rem))] overflow-y-auto"
      >
        <div className="flex items-start justify-between gap-3 mb-4">
          <div className="flex items-center gap-3 min-w-0">
            <span className="inline-flex items-center justify-center w-9 h-9 rounded-full bg-brand-600/20 text-brand-300 border border-brand-500/30 shrink-0">
              {step + 1}
            </span>
            <div className="min-w-0">
              <p className="text-[11px] uppercase tracking-wider text-gray-500 mb-0.5">
                Quick tour · {step + 1} of {STEPS.length}
              </p>
              <h2 id="coach-marks-title" className="text-lg font-semibold text-gray-100 flex items-center gap-2">
                <Icon size={18} className="text-brand-400 shrink-0" />
                {current.title}
              </h2>
            </div>
          </div>
          <button
            type="button"
            onClick={dismiss}
            className="p-1.5 rounded-lg text-gray-500 hover:text-gray-200 hover:bg-gray-800 transition-colors"
            aria-label="Dismiss tour"
          >
            <X size={16} />
          </button>
        </div>

        <p className="text-sm text-gray-400 leading-relaxed mb-5">{current.body}</p>

        <div className="flex items-center justify-between gap-3">
          <button
            type="button"
            onClick={dismiss}
            className="text-sm text-gray-500 hover:text-gray-300 transition-colors px-2 py-1.5"
          >
            Skip
          </button>
          <div className="flex items-center gap-2">
            <Link
              to={current.to}
              onClick={isLast ? dismiss : undefined}
              className="text-sm text-brand-400 hover:text-brand-300 px-2 py-1.5"
            >
              Open {current.title}
            </Link>
            <button
              type="button"
              onClick={() => {
                if (isLast) dismiss();
                else setStep((s) => s + 1);
              }}
              className="px-4 py-2 text-sm font-medium rounded-lg bg-brand-600 text-white hover:bg-brand-500 transition-colors"
            >
              {isLast ? "Done" : "Next"}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
