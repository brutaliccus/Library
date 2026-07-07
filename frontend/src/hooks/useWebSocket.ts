import { useEffect, useRef, useState } from "react";
import { isWsConnected, subscribeWs, type WSMessage } from "./wsClient";

export type { WSMessage };

export function useWebSocket(onMessage?: (msg: WSMessage) => void) {
  const handlerRef = useRef(onMessage);
  handlerRef.current = onMessage;

  const [connected, setConnected] = useState(isWsConnected);

  useEffect(() => {
    if (!onMessage) return;

    const unsubscribe = subscribeWs((msg) => handlerRef.current?.(msg));

    const interval = window.setInterval(() => {
      setConnected(isWsConnected());
    }, 1000);

    setConnected(isWsConnected());

    return () => {
      window.clearInterval(interval);
      unsubscribe();
    };
  }, [!!onMessage]);

  return { connected };
}
