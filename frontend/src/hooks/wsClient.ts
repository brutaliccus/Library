import {
  AUTH_TOKEN_REFRESHED_EVENT,
  refreshAccessTokenIfNeeded,
} from "../api/client";
import { getApiOrigin } from "../api/instanceUrl";

export interface WSMessage {
  type: string;
  request_id?: number;
  status?: string;
  detail?: string;
  title?: string;
  progress_percent?: number | null;
  progress_bytes?: number | null;
  progress_total_bytes?: number | null;
  progress_speed_bps?: number | null;
}

type Listener = (msg: WSMessage) => void;

let socket: WebSocket | null = null;
let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
let intentionalClose = false;
let connecting = false;
const listeners = new Set<Listener>();

function wsUrl(token: string): string {
  const origin = getApiOrigin() || window.location.origin;
  let host = window.location.host;
  let protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  try {
    const u = new URL(origin);
    host = u.host;
    protocol = u.protocol === "https:" ? "wss:" : "ws:";
  } catch {
    // keep window.location
  }
  return `${protocol}//${host}/api/requests/ws?token=${encodeURIComponent(token)}`;
}

function scheduleReconnect(delayMs = 5000) {
  if (intentionalClose || reconnectTimer) return;
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    void connect();
  }, delayMs);
}

async function connect() {
  if (connecting) return;
  if (socket && (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING)) {
    return;
  }

  connecting = true;
  try {
    const token = await refreshAccessTokenIfNeeded();
    if (!token) return;

    intentionalClose = false;
    const ws = new WebSocket(wsUrl(token));

    ws.onopen = () => {
      socket = ws;
    };

    ws.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data) as WSMessage;
        for (const listener of listeners) listener(data);
      } catch {
        /* ignore malformed messages */
      }
    };

    ws.onclose = (ev) => {
      if (socket === ws) socket = null;
      if (intentionalClose) return;
      if (ev.code === 1008) {
        void refreshAccessTokenIfNeeded(0).then((t) => {
          if (t) scheduleReconnect(500);
        });
        return;
      }
      scheduleReconnect();
    };

    ws.onerror = () => {
      /* onclose handles reconnect */
    };

    socket = ws;
  } finally {
    connecting = false;
  }
}

function disconnect() {
  intentionalClose = true;
  if (reconnectTimer) {
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }
  const ws = socket;
  socket = null;
  if (!ws) return;
  if (ws.readyState === WebSocket.CONNECTING) {
    ws.onopen = () => ws.close();
    ws.onclose = null;
    return;
  }
  if (ws.readyState === WebSocket.OPEN) ws.close();
}

function forceReconnect() {
  disconnect();
  intentionalClose = false;
  void connect();
}

if (typeof window !== "undefined") {
  window.addEventListener(AUTH_TOKEN_REFRESHED_EVENT, () => {
    if (listeners.size > 0) forceReconnect();
  });
}

export function subscribeWs(listener: Listener): () => void {
  listeners.add(listener);
  void connect();
  return () => {
    listeners.delete(listener);
    if (listeners.size === 0) disconnect();
  };
}

export function isWsConnected(): boolean {
  return socket?.readyState === WebSocket.OPEN;
}
