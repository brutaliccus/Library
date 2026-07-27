import { useState, useEffect } from "react";
import { Check, Download, HelpCircle, X } from "lucide-react";

const STORAGE_KEY = "browse-badge-legend-dismissed";

export default function BadgeLegend() {
  const [visible, setVisible] = useState(false);

  useEffect(() => {
    try {
      if (!localStorage.getItem(STORAGE_KEY)) setVisible(true);
    } catch {
      setVisible(true);
    }
  }, []);

  const dismiss = () => {
    setVisible(false);
    try {
      localStorage.setItem(STORAGE_KEY, "1");
    } catch {
      /* ignore */
    }
  };

  if (!visible) return null;

  return (
    <div className="mb-6 rounded-xl border border-gray-800 bg-gray-900/80 px-4 py-3 flex flex-col sm:flex-row sm:items-center gap-3">
      <div className="flex-1 min-w-0">
        <p className="text-sm font-medium text-gray-200 mb-2">Cover badges</p>
        <ul className="flex flex-wrap gap-x-4 gap-y-2 text-xs text-gray-400">
          <li className="inline-flex items-center gap-1.5">
            <span className="inline-flex items-center justify-center w-5 h-5 rounded-full bg-emerald-900/80 text-emerald-400">
              <Check size={12} />
            </span>
            Already in your library
          </li>
          <li className="inline-flex items-center gap-1.5">
            <span className="inline-flex items-center justify-center w-5 h-5 rounded-full bg-sky-900/80 text-sky-400">
              <Download size={12} />
            </span>
            Available to download
          </li>
          <li className="inline-flex items-center gap-1.5">
            <span className="inline-flex items-center justify-center w-5 h-5 rounded-full bg-amber-900/80 text-amber-300">
              <HelpCircle size={12} />
            </span>
            In catalog — not cached yet
          </li>
        </ul>
      </div>
      <button
        type="button"
        onClick={dismiss}
        className="self-end sm:self-center p-1.5 rounded-lg text-gray-500 hover:text-gray-200 hover:bg-gray-800 transition-colors"
        aria-label="Dismiss badge legend"
      >
        <X size={16} />
      </button>
    </div>
  );
}
