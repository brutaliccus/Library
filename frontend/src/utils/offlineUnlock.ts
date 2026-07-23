/**
 * Device-local offline unlock (PIN and/or biometric) for a saved library session.
 *
 * Scope: per library origin + account email on this device. Never sent to the server.
 * PIN is stored as salted SHA-256 only — never plaintext.
 * Biometric uses WebAuthn platform authenticators when available (fingerprint/face),
 * with PIN as the always-available fallback.
 */

const STORE_KEY = "offline-unlock-v1";
/** One-time "set this later" dismissals for existing accounts (per origin+email). */
const PROMPT_DISMISS_KEY = "offline-unlock-prompt-dismissed-v1";

export interface OfflineUnlockRecord {
  origin: string;
  email: string;
  salt: string;
  pinHash: string;
  biometricEnabled: boolean;
  /** Base64url credential id when WebAuthn enrolled */
  webauthnCredentialId?: string;
  createdAt: number;
  updatedAt: number;
}

type Store = Record<string, OfflineUnlockRecord>;

function normalizeOrigin(origin: string): string {
  return origin.replace(/\/+$/, "");
}

function normalizeEmail(email: string): string {
  return email.trim().toLowerCase();
}

function recordKey(origin: string, email: string): string {
  return `${normalizeOrigin(origin)}::${normalizeEmail(email)}`;
}

function readStore(): Store {
  try {
    const raw = localStorage.getItem(STORE_KEY);
    if (!raw) return {};
    const parsed = JSON.parse(raw) as Store;
    return parsed && typeof parsed === "object" ? parsed : {};
  } catch {
    return {};
  }
}

function writeStore(store: Store): void {
  try {
    localStorage.setItem(STORE_KEY, JSON.stringify(store));
  } catch {
    /* quota / private mode */
  }
}

function bytesToBase64url(bytes: ArrayBuffer | Uint8Array): string {
  const arr = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes);
  let bin = "";
  for (let i = 0; i < arr.length; i++) bin += String.fromCharCode(arr[i]);
  return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function base64urlToBytes(s: string): Uint8Array {
  const pad = "=".repeat((4 - (s.length % 4)) % 4);
  const b64 = (s + pad).replace(/-/g, "+").replace(/_/g, "/");
  const bin = atob(b64);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

function randomSalt(): string {
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  return bytesToBase64url(bytes);
}

async function hashPin(pin: string, salt: string): Promise<string> {
  const enc = new TextEncoder();
  // salt|pin — salt is random per enrollment; never store plaintext PIN.
  const data = enc.encode(`${salt}|${pin}`);
  const digest = await crypto.subtle.digest("SHA-256", data);
  return bytesToBase64url(digest);
}

export function getOfflineUnlock(
  origin: string,
  email: string
): OfflineUnlockRecord | null {
  if (!origin || !email) return null;
  return readStore()[recordKey(origin, email)] || null;
}

export function hasOfflineUnlock(origin: string, email: string): boolean {
  return !!getOfflineUnlock(origin, email)?.pinHash;
}

function readDismissed(): Record<string, boolean> {
  try {
    const raw = localStorage.getItem(PROMPT_DISMISS_KEY);
    if (!raw) return {};
    const parsed = JSON.parse(raw) as Record<string, boolean>;
    return parsed && typeof parsed === "object" ? parsed : {};
  } catch {
    return {};
  }
}

/** True when this device already has a PIN for the account. */
export function wasOfflineUnlockPromptDismissed(
  origin: string,
  email: string
): boolean {
  if (!origin || !email) return false;
  return !!readDismissed()[recordKey(origin, email)];
}

/** Mark one-time setup prompt as dismissed (Settings remains available). */
export function dismissOfflineUnlockPrompt(origin: string, email: string): void {
  if (!origin || !email) return;
  try {
    const store = readDismissed();
    store[recordKey(origin, email)] = true;
    localStorage.setItem(PROMPT_DISMISS_KEY, JSON.stringify(store));
  } catch {
    /* quota / private mode */
  }
}

/** Show a skippable setup prompt once for existing accounts with no PIN. */
export function shouldPromptOfflineUnlockSetup(
  origin: string,
  email: string
): boolean {
  if (!origin || !email) return false;
  if (hasOfflineUnlock(origin, email)) return false;
  return !wasOfflineUnlockPromptDismissed(origin, email);
}

export async function enrollOfflineUnlock(opts: {
  origin: string;
  email: string;
  pin: string;
  enableBiometric?: boolean;
}): Promise<OfflineUnlockRecord> {
  const origin = normalizeOrigin(opts.origin);
  const email = normalizeEmail(opts.email);
  if (!origin || !email) throw new Error("Missing library or account for offline unlock");
  if (!/^\d{4,8}$/.test(opts.pin)) {
    throw new Error("PIN must be 4–8 digits");
  }

  const salt = randomSalt();
  const pinHash = await hashPin(opts.pin, salt);
  const now = Date.now();
  const existing = getOfflineUnlock(origin, email);

  let biometricEnabled = false;
  let webauthnCredentialId = existing?.webauthnCredentialId;

  if (opts.enableBiometric) {
    const enrolled = await enrollWebAuthnBiometric(origin, email);
    if (enrolled) {
      biometricEnabled = true;
      webauthnCredentialId = enrolled;
    }
  }

  const record: OfflineUnlockRecord = {
    origin,
    email,
    salt,
    pinHash,
    biometricEnabled,
    webauthnCredentialId,
    createdAt: existing?.createdAt || now,
    updatedAt: now,
  };
  const store = readStore();
  store[recordKey(origin, email)] = record;
  writeStore(store);
  return record;
}

export async function verifyOfflinePin(
  origin: string,
  email: string,
  pin: string
): Promise<boolean> {
  const rec = getOfflineUnlock(origin, email);
  if (!rec) return false;
  const hash = await hashPin(pin, rec.salt);
  return hash === rec.pinHash;
}

export function clearOfflineUnlock(origin: string, email: string): void {
  const store = readStore();
  delete store[recordKey(origin, email)];
  writeStore(store);
}

export async function biometricAvailable(): Promise<boolean> {
  try {
    if (typeof window === "undefined" || !window.PublicKeyCredential) return false;
    if (typeof PublicKeyCredential.isUserVerifyingPlatformAuthenticatorAvailable !== "function") {
      return false;
    }
    return await PublicKeyCredential.isUserVerifyingPlatformAuthenticatorAvailable();
  } catch {
    return false;
  }
}

async function enrollWebAuthnBiometric(
  origin: string,
  email: string
): Promise<string | null> {
  try {
    if (!(await biometricAvailable())) return null;
    const userId = new TextEncoder().encode(`${origin}|${email}`);
    const challenge = new Uint8Array(32);
    crypto.getRandomValues(challenge);
    const cred = (await navigator.credentials.create({
      publicKey: {
        challenge,
        rp: { name: "Library Offline Unlock", id: window.location.hostname },
        user: {
          id: userId,
          name: email,
          displayName: email,
        },
        pubKeyCredParams: [
          { type: "public-key", alg: -7 },
          { type: "public-key", alg: -257 },
        ],
        authenticatorSelection: {
          authenticatorAttachment: "platform",
          userVerification: "required",
          residentKey: "preferred",
        },
        timeout: 60_000,
        attestation: "none",
      },
    })) as PublicKeyCredential | null;
    if (!cred) return null;
    return bytesToBase64url(cred.rawId);
  } catch {
    return null;
  }
}

/** Prompt platform biometric; returns true when the user verifies successfully. */
export async function verifyOfflineBiometric(
  origin: string,
  email: string
): Promise<boolean> {
  const rec = getOfflineUnlock(origin, email);
  if (!rec?.biometricEnabled || !rec.webauthnCredentialId) return false;
  try {
    const challenge = new Uint8Array(32);
    crypto.getRandomValues(challenge);
    const assertion = await navigator.credentials.get({
      publicKey: {
        challenge,
        allowCredentials: [
          {
            type: "public-key",
            id: base64urlToBytes(rec.webauthnCredentialId) as unknown as BufferSource,
            transports: ["internal"],
          },
        ],
        userVerification: "required",
        timeout: 60_000,
      },
    });
    return !!assertion;
  } catch {
    return false;
  }
}

export async function setBiometricEnabled(
  origin: string,
  email: string,
  enabled: boolean
): Promise<boolean> {
  const rec = getOfflineUnlock(origin, email);
  if (!rec) return false;
  if (enabled) {
    const id = await enrollWebAuthnBiometric(origin, email);
    if (!id) return false;
    rec.biometricEnabled = true;
    rec.webauthnCredentialId = id;
  } else {
    rec.biometricEnabled = false;
    delete rec.webauthnCredentialId;
  }
  rec.updatedAt = Date.now();
  const store = readStore();
  store[recordKey(origin, email)] = rec;
  writeStore(store);
  return true;
}
