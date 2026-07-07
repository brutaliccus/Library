import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { RotateCcw, EyeOff } from "lucide-react";

export interface ContinueMenuTarget {
  kind: "abs" | "rd" | "ebook";
  id: string | number;
  title: string;
  coverUrl?: string;
  anchorX: number;
  anchorY: number;
}

interface Props {
  target: ContinueMenuTarget | null;
  onClose: () => void;
  onClearProgress: (target: ContinueMenuTarget) => void;
  onHide: (target: ContinueMenuTarget) => void;
}

const MENU_W = 220;
const MENU_H = 88;
const PAD = 8;

/** Compact context menu anchored at the long-press / right-click point. */
export default function ContinueItemMenu({ target, onClose, onClearProgress, onHide }: Props) {
  const menuRef = useRef<HTMLDivElement>(null);
  const [pos, setPos] = useState({ left: 0, top: 0 });

  useLayoutEffect(() => {
    if (!target) return;
    let left = target.anchorX - MENU_W / 2;
    let top = target.anchorY - MENU_H - 10;
    // Flip below the finger if it would clip off the top
    if (top < PAD) top = target.anchorY + 10;
    left = Math.max(PAD, Math.min(left, window.innerWidth - MENU_W - PAD));
    top = Math.max(PAD, Math.min(top, window.innerHeight - MENU_H - PAD));
    setPos({ left, top });
  }, [target]);

  useEffect(() => {
    if (!target) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    const onPointer = (e: PointerEvent) => {
      if (menuRef.current?.contains(e.target as Node)) return;
      onClose();
    };
    window.addEventListener("keydown", onKey);
    // Capture phase so we close before the tile's click fires
    window.addEventListener("pointerdown", onPointer, true);
    return () => {
      window.removeEventListener("keydown", onKey);
      window.removeEventListener("pointerdown", onPointer, true);
    };
  }, [target, onClose]);

  if (!target) return null;

  const shelfName = target.kind === "ebook" ? "Continue Reading" : "Continue Listening";

  return (
    <div
      ref={menuRef}
      className="fixed z-[110] w-[220px] bg-gray-900 border border-gray-600 rounded-lg shadow-xl py-1"
      style={{ left: pos.left, top: pos.top }}
      onClick={(e) => e.stopPropagation()}
    >
      <p className="px-3 py-1.5 text-[11px] text-gray-500 truncate border-b border-gray-800">
        {target.title}
      </p>
      <button
        onClick={() => onHide(target)}
        className="w-full flex items-center gap-2.5 px-3 py-2 text-left hover:bg-gray-800 transition-colors"
      >
        <EyeOff size={14} className="text-gray-400 shrink-0" />
        <span className="text-sm text-gray-100">Hide from {shelfName}</span>
      </button>
      <button
        onClick={() => onClearProgress(target)}
        className="w-full flex items-center gap-2.5 px-3 py-2 text-left hover:bg-red-900/30 transition-colors"
      >
        <RotateCcw size={14} className="text-red-400 shrink-0" />
        <span className="text-sm text-gray-100">Clear progress</span>
      </button>
    </div>
  );
}
