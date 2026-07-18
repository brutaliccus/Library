import { useEffect, useState } from "react";
import {
  getStoredInstanceUrl,
  isNativeApp,
  normalizeInstanceUrl,
  setInstanceUrl,
} from "../api/instanceUrl";
import { applyApiBaseUrl } from "../api/client";

interface ServerUrlFieldProps {
  value?: string;
  onChange?: (value: string) => void;
  /** Called after a valid URL is saved (and API base URL updated). */
  onSaved?: (url: string) => void;
  /** When true, show even on web (usually native-only). */
  forceShow?: boolean;
  className?: string;
  autoFocus?: boolean;
  required?: boolean;
}

/**
 * Library server URL input for the Android APK (and optional web use).
 * Saves to localStorage and refreshes the axios base URL on blur / commit.
 */
export default function ServerUrlField({
  value: controlled,
  onChange,
  onSaved,
  forceShow = false,
  className = "",
  autoFocus = false,
  required = true,
}: ServerUrlFieldProps) {
  const show = forceShow || isNativeApp();
  const [internal, setInternal] = useState(() => getStoredInstanceUrl() || "");
  const value = controlled !== undefined ? controlled : internal;
  const [error, setError] = useState("");

  useEffect(() => {
    if (controlled === undefined) {
      setInternal(getStoredInstanceUrl() || "");
    }
  }, [controlled]);

  if (!show) return null;

  const setValue = (next: string) => {
    if (controlled === undefined) setInternal(next);
    onChange?.(next);
    setError("");
  };

  const persist = (raw: string): string | null => {
    const normalized = normalizeInstanceUrl(raw);
    if (!normalized) {
      setError("Enter a valid HTTPS URL, e.g. https://library.example.com");
      return null;
    }
    try {
      const saved = setInstanceUrl(normalized);
      applyApiBaseUrl();
      setValue(saved);
      setError("");
      onSaved?.(saved);
      return saved;
    } catch (e) {
      setError(e instanceof Error ? e.message : "Invalid URL");
      return null;
    }
  };

  return (
    <div className={className}>
      <label className="block text-sm font-medium text-gray-300 mb-1">
        Library server URL {required ? "*" : ""}
      </label>
      <input
        type="url"
        inputMode="url"
        autoCapitalize="none"
        autoCorrect="off"
        spellCheck={false}
        placeholder="https://library.example.com"
        value={value}
        autoFocus={autoFocus}
        required={required}
        onChange={(e) => setValue(e.target.value)}
        onBlur={() => {
          if (value.trim()) persist(value);
        }}
        className="w-full px-3 py-2.5 bg-gray-900 border border-gray-600 rounded-lg text-gray-100 focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent placeholder:text-gray-500"
      />
      <p className="text-[11px] text-gray-500 mt-1">
        Your self-hosted Library address (must be reachable from this device, usually HTTPS).
      </p>
      {error && <p className="text-xs text-red-400 mt-1">{error}</p>}
    </div>
  );
}

/** Validate + save; returns normalized URL or null. */
export function commitServerUrl(raw: string): string | null {
  const normalized = normalizeInstanceUrl(raw);
  if (!normalized) return null;
  setInstanceUrl(normalized);
  applyApiBaseUrl();
  return normalized;
}
