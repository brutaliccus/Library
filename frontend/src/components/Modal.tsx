import { useEffect } from "react";
import { X } from "lucide-react";

interface ModalProps {
  title: string;
  children: React.ReactNode;
  onClose: () => void;
  show: boolean;
  /** lg = wide downloads list; xl = Quick Review wizard */
  size?: "md" | "lg" | "xl";
}

export default function Modal({ title, children, onClose, show, size = "md" }: ModalProps) {
  useEffect(() => {
    if (!show) return;
    const handle = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", handle);
    return () => window.removeEventListener("keydown", handle);
  }, [show, onClose]);

  if (!show) return null;

  const maxWidth =
    size === "xl" ? "max-w-4xl" : size === "lg" ? "max-w-3xl" : "max-w-md";

  return (
    <div
      className="fixed inset-0 z-[200] flex items-center justify-center p-2 sm:p-4"
      role="dialog"
      aria-modal="true"
      aria-labelledby="modal-title"
    >
      <div
        className="absolute inset-0 bg-black/60"
        onClick={onClose}
        aria-hidden="true"
      />
      <div
        className={`relative bg-gray-800 border border-gray-700 rounded-xl shadow-xl w-full max-h-[92vh] flex flex-col ${maxWidth}`}
      >
        <div className="flex items-center justify-between p-4 border-b border-gray-700 shrink-0">
          <h2 id="modal-title" className="text-lg font-semibold text-gray-100 pr-2">
            {title}
          </h2>
          <button
            type="button"
            onClick={onClose}
            className="p-1 rounded-lg text-gray-400 hover:bg-gray-700 hover:text-gray-200 shrink-0"
            aria-label="Close"
          >
            <X size={20} />
          </button>
        </div>
        <div className="p-4 overflow-y-auto min-h-0 flex-1">{children}</div>
      </div>
    </div>
  );
}
