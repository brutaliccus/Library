import { WifiOff } from "lucide-react";
import { useAuth } from "../hooks/useAuth";
import { useOnlineStatus } from "../hooks/useOnlineStatus";

/** Compact banner when viewing a cached library without a live connection. */
export default function OfflineBanner() {
  const online = useOnlineStatus();
  const { user, offlineSession } = useAuth();
  if (!user) return null;
  if (online && !offlineSession) return null;

  return (
    <div className="bg-amber-950/80 border-b border-amber-800/60 text-amber-200 text-xs px-4 py-2 flex items-center justify-center gap-2">
      <WifiOff size={14} className="shrink-0" />
      <span>
        {online
          ? "Showing cached library — server unreachable. Online features are limited."
          : "You're offline — showing your cached library. Downloaded books play on this device."}
      </span>
    </div>
  );
}
