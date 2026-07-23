import { useEffect, useState } from "react";
import { useLocation } from "react-router-dom";
import { useAuth } from "../hooks/useAuth";
import { currentOrigin } from "../api/libraryRegistry";
import { useToast } from "../contexts/ToastContext";
import { useOnlineStatus } from "../hooks/useOnlineStatus";
import OfflineUnlockModal from "./OfflineUnlockModal";
import {
  dismissOfflineUnlockPrompt,
  shouldPromptOfflineUnlockSetup,
} from "../utils/offlineUnlock";

const SKIP_PATH_PREFIXES = [
  "/libraries",
  "/onboarding",
  "/login",
  "/join",
  "/change-password",
  "/set-email",
];

/**
 * One-time skippable prompt for existing accounts that never enrolled a PIN.
 * New users set PIN during onboarding; this covers next login for older accounts.
 */
export default function OfflineUnlockSetupPrompt() {
  const { user, sessionReady, offlineSession } = useAuth();
  const online = useOnlineStatus();
  const { toast } = useToast();
  const location = useLocation();
  const [open, setOpen] = useState(false);

  const origin = currentOrigin();
  const email = user?.email || localStorage.getItem("user_email") || "";

  useEffect(() => {
    if (!sessionReady || !user || !online || offlineSession) {
      setOpen(false);
      return;
    }
    if (SKIP_PATH_PREFIXES.some((p) => location.pathname === p || location.pathname.startsWith(`${p}/`))) {
      setOpen(false);
      return;
    }
    if (!origin || !email) {
      setOpen(false);
      return;
    }
    setOpen(shouldPromptOfflineUnlockSetup(origin, email));
  }, [sessionReady, user, online, offlineSession, location.pathname, origin, email]);

  if (!open || !origin || !email) return null;

  const dismiss = () => {
    dismissOfflineUnlockPrompt(origin, email);
    setOpen(false);
  };

  return (
    <OfflineUnlockModal
      mode="setup"
      libraryName="Your library"
      origin={origin}
      email={email}
      allowSkip
      onClose={dismiss}
      onSkip={dismiss}
      onUnlocked={() => {
        setOpen(false);
        toast("Offline unlock ready", "success");
      }}
    />
  );
}
