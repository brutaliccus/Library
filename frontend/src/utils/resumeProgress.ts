/**
 * Pick resume position: online prefers server; offline prefers local.
 * When both exist online, prefer the newer ``updatedAt`` (ms).
 */
export function pickResumeSeconds(opts: {
  serverSeconds: number;
  serverUpdatedAtMs?: number | null;
  localSeconds?: number | null;
  localUpdatedAtMs?: number | null;
  offline?: boolean;
}): number {
  const server = Math.max(0, opts.serverSeconds || 0);
  const local = opts.localSeconds != null ? Math.max(0, opts.localSeconds) : null;
  if (local == null) return server;
  if (opts.offline) return local;

  const serverTs = opts.serverUpdatedAtMs ?? 0;
  const localTs = opts.localUpdatedAtMs ?? 0;
  if (localTs > serverTs + 5_000) return local;
  if (serverTs > localTs + 5_000) return server;
  // Same-ish timestamps: take the farther position (more progress).
  return Math.max(server, local);
}
