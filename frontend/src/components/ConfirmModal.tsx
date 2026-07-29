import { Loader2 } from "lucide-react";
import Modal from "./Modal";

export type ConfirmVariant = "danger" | "warning" | "primary";

type ConfirmModalProps = {
  show: boolean;
  title: string;
  body: React.ReactNode;
  confirmLabel?: string;
  cancelLabel?: string;
  variant?: ConfirmVariant;
  busy?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
};

const CONFIRM_BTN: Record<ConfirmVariant, string> = {
  danger: "bg-red-600 text-white hover:bg-red-500",
  warning: "bg-amber-600 text-white hover:bg-amber-500",
  primary: "bg-brand-600 text-white hover:bg-brand-500",
};

/** Themed confirm dialog — prefer over window.confirm for admin / Sweep actions. */
export default function ConfirmModal({
  show,
  title,
  body,
  confirmLabel = "Confirm",
  cancelLabel = "Cancel",
  variant = "primary",
  busy = false,
  onConfirm,
  onCancel,
}: ConfirmModalProps) {
  return (
    <Modal title={title} show={show} onClose={busy ? () => undefined : onCancel}>
      <div className="text-sm text-gray-400 mb-4">{body}</div>
      <div className="flex gap-2 justify-end">
        <button
          type="button"
          onClick={onCancel}
          disabled={busy}
          className="px-3 py-1.5 text-gray-300 hover:bg-gray-700 rounded-lg disabled:opacity-50"
        >
          {cancelLabel}
        </button>
        <button
          type="button"
          onClick={onConfirm}
          disabled={busy}
          className={`inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg disabled:opacity-50 ${CONFIRM_BTN[variant]}`}
        >
          {busy ? <Loader2 size={14} className="animate-spin" /> : null}
          {confirmLabel}
        </button>
      </div>
    </Modal>
  );
}
