import { useCallback, useRef } from "react";

const LONG_PRESS_MS = 500;
const MOVE_TOLERANCE_PX = 12;

/**
 * Long-press (touch) + right-click (desktop) detection.
 * Passes the press coordinates so menus can anchor at the pointer.
 */
export function useLongPress(onLongPress: (point: { x: number; y: number }) => void) {
  const timerRef = useRef<ReturnType<typeof setTimeout>>();
  const startPos = useRef<{ x: number; y: number } | null>(null);
  const firedRef = useRef(false);

  const clear = useCallback(() => {
    if (timerRef.current) clearTimeout(timerRef.current);
    timerRef.current = undefined;
    startPos.current = null;
  }, []);

  const fire = useCallback(
    (x: number, y: number) => {
      firedRef.current = true;
      onLongPress({ x, y });
    },
    [onLongPress]
  );

  const onTouchStart = useCallback(
    (e: React.TouchEvent) => {
      firedRef.current = false;
      const t = e.touches[0];
      startPos.current = { x: t.clientX, y: t.clientY };
      timerRef.current = setTimeout(() => {
        if (startPos.current) fire(startPos.current.x, startPos.current.y);
      }, LONG_PRESS_MS);
    },
    [fire]
  );

  const onTouchMove = useCallback(
    (e: React.TouchEvent) => {
      if (!startPos.current) return;
      const t = e.touches[0];
      if (
        Math.abs(t.clientX - startPos.current.x) > MOVE_TOLERANCE_PX ||
        Math.abs(t.clientY - startPos.current.y) > MOVE_TOLERANCE_PX
      ) {
        clear();
      }
    },
    [clear]
  );

  const onTouchEnd = useCallback(() => clear(), [clear]);

  const onContextMenu = useCallback(
    (e: React.MouseEvent) => {
      e.preventDefault();
      fire(e.clientX, e.clientY);
    },
    [fire]
  );

  const onClickCapture = useCallback((e: React.MouseEvent) => {
    if (firedRef.current) {
      e.preventDefault();
      e.stopPropagation();
      firedRef.current = false;
    }
  }, []);

  return { onTouchStart, onTouchMove, onTouchEnd, onContextMenu, onClickCapture };
}
