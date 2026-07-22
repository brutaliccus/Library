import { useEffect, useState } from "react";
import { isLikelyOffline, subscribeOnlineStatus } from "../utils/networkStatus";

/** Reactive online flag (starts from navigator.onLine). */
export function useOnlineStatus(): boolean {
  const [online, setOnline] = useState(() => !isLikelyOffline());
  useEffect(() => subscribeOnlineStatus(setOnline), []);
  return online;
}
