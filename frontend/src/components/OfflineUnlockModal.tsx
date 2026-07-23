import { useEffect, useState } from "react";
import { Fingerprint, KeyRound, Loader2, X } from "lucide-react";
import {
  biometricAvailable,
  enrollOfflineUnlock,
  verifyOfflineBiometric,
  verifyOfflinePin,
} from "../utils/offlineUnlock";

type Mode = "setup" | "unlock";

interface Props {
  mode: Mode;
  libraryName: string;
  origin: string;
  email: string;
  onClose: () => void;
  onUnlocked: () => void;
  onSetupComplete?: () => void;
  /** Setup only: hide dismiss controls (used during account onboarding). */
  required?: boolean;
  /** Setup only: show “set later in Settings” (existing accounts, one-time). */
  allowSkip?: boolean;
  onSkip?: () => void;
}

export default function OfflineUnlockModal({
  mode,
  libraryName,
  origin,
  email,
  onClose,
  onUnlocked,
  onSetupComplete,
  required = false,
  allowSkip = false,
  onSkip,
}: Props) {
  const [pin, setPin] = useState("");
  const [pinConfirm, setPinConfirm] = useState("");
  const [enableBio, setEnableBio] = useState(false);
  const [bioOk, setBioOk] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [triedBio, setTriedBio] = useState(false);

  useEffect(() => {
    void biometricAvailable().then(setBioOk);
  }, []);

  useEffect(() => {
    if (mode !== "unlock" || !bioOk || triedBio) return;
    setTriedBio(true);
    void (async () => {
      setBusy(true);
      setError("");
      try {
        const ok = await verifyOfflineBiometric(origin, email);
        if (ok) {
          onUnlocked();
          return;
        }
      } catch {
        /* fall through to PIN */
      } finally {
        setBusy(false);
      }
    })();
  }, [mode, bioOk, triedBio, origin, email, onUnlocked]);

  const submitSetup = async () => {
    setError("");
    if (pin !== pinConfirm) {
      setError("PINs do not match");
      return;
    }
    setBusy(true);
    try {
      await enrollOfflineUnlock({
        origin,
        email,
        pin,
        enableBiometric: enableBio && bioOk,
      });
      onSetupComplete?.();
      onUnlocked();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not save PIN");
    } finally {
      setBusy(false);
    }
  };

  const submitUnlock = async () => {
    setError("");
    setBusy(true);
    try {
      const ok = await verifyOfflinePin(origin, email, pin);
      if (!ok) {
        setError("Incorrect PIN");
        return;
      }
      onUnlocked();
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="fixed inset-0 z-[80] flex items-end sm:items-center justify-center bg-black/60 p-4">
      <div className="w-full max-w-md rounded-xl border border-gray-800 bg-gray-900 p-5 shadow-xl">
        <div className="flex items-center justify-between mb-3">
          <h2 className="text-sm font-semibold text-gray-100">
            {mode === "setup" ? "Set up offline unlock" : `Open offline — ${libraryName}`}
          </h2>
          {!(mode === "setup" && required) && (
            <button
              type="button"
              onClick={onClose}
              className="p-1 text-gray-500 hover:text-gray-200"
              aria-label="Close"
            >
              <X size={18} />
            </button>
          )}
        </div>

        <p className="text-xs text-gray-400 mb-4">
          {mode === "setup"
            ? "Create a local PIN to open this library when you're offline. It stays on this device and is never sent to the server."
            : "Unlock your saved session on this device. Your library password is not used offline."}
        </p>

        {error && (
          <div className="mb-3 p-2 bg-red-900/30 text-red-400 text-xs rounded-lg">{error}</div>
        )}

        <label className="block text-[11px] text-gray-500 mb-1">
          {mode === "setup" ? "Choose a PIN (4–8 digits)" : "PIN"}
        </label>
        <input
          type="password"
          inputMode="numeric"
          autoComplete="one-time-code"
          value={pin}
          onChange={(e) => setPin(e.target.value.replace(/\D/g, "").slice(0, 8))}
          className="w-full px-3 py-2 bg-gray-800 border border-gray-700 rounded-lg text-sm text-gray-100 mb-3 tracking-widest"
          autoFocus
        />

        {mode === "setup" && (
          <>
            <label className="block text-[11px] text-gray-500 mb-1">Confirm PIN</label>
            <input
              type="password"
              inputMode="numeric"
              autoComplete="one-time-code"
              value={pinConfirm}
              onChange={(e) => setPinConfirm(e.target.value.replace(/\D/g, "").slice(0, 8))}
              className="w-full px-3 py-2 bg-gray-800 border border-gray-700 rounded-lg text-sm text-gray-100 mb-3 tracking-widest"
            />
            {bioOk && (
              <label className="flex items-center gap-2 text-xs text-gray-300 mb-4 cursor-pointer">
                <input
                  type="checkbox"
                  checked={enableBio}
                  onChange={(e) => setEnableBio(e.target.checked)}
                  className="rounded border-gray-600"
                />
                <Fingerprint size={14} className="text-brand-400" />
                Also unlock with fingerprint / face
              </label>
            )}
          </>
        )}

        <button
          type="button"
          disabled={busy || pin.length < 4 || (mode === "setup" && pinConfirm.length < 4)}
          onClick={() => void (mode === "setup" ? submitSetup() : submitUnlock())}
          className="w-full flex items-center justify-center gap-2 py-2 bg-brand-600 text-white text-sm font-medium rounded-lg hover:bg-brand-500 disabled:opacity-50"
        >
          {busy ? <Loader2 size={14} className="animate-spin" /> : <KeyRound size={14} />}
          {busy
            ? mode === "setup"
              ? "Saving…"
              : "Unlocking…"
            : mode === "setup"
              ? "Save & continue"
              : "Unlock"}
        </button>

        {mode === "setup" && allowSkip && !required && (
          <button
            type="button"
            disabled={busy}
            onClick={() => (onSkip ? onSkip() : onClose())}
            className="w-full mt-2 py-2 text-xs text-gray-500 hover:text-gray-300 disabled:opacity-50"
          >
            You can set this later in Settings
          </button>
        )}

        {mode === "unlock" && bioOk && (
          <button
            type="button"
            disabled={busy}
            onClick={() => {
              setTriedBio(false);
            }}
            className="w-full mt-2 flex items-center justify-center gap-2 py-2 text-sm text-gray-300 border border-gray-700 rounded-lg hover:bg-gray-800 disabled:opacity-50"
          >
            <Fingerprint size={14} />
            Use biometric
          </button>
        )}
      </div>
    </div>
  );
}
