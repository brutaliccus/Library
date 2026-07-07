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
const listeners = new Set<Listener>();

function wsUrl(token: string): string {
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${protocol}//${window.location.host}/api/requests/ws?token=${encodeURIComponent(token)}`;
}

function scheduleReconnect() {
  if (intentionalClose || reconnectTimer) return;
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    connect();
  }, 5000);
}

function connect() {
  const token = localStorage.getItem("access_token");
  if (!token) return;

  if (socket && (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING)) {
    return;
  }

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
      // ignore malformed messages
    }
  };

  ws.onclose = () => {
    if (socket === ws) socket = null;
    if (!intentionalClose) scheduleReconnect();
  };

  ws.onerror = () => {
    // onclose handles reconnect
  };

  socket = ws;
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

export function subscribeWs(listener: Listener): () => void {
  listeners.add(listener);
  connect();
  return () => {
    listeners.delete(listener);
    if (listeners.size === 0) disconnect();
  };
}

export function isWsConnected(): boolean {
  return socket?.readyState === WebSocket.OPEN;
}
