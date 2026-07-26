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

function webCrypto(): Crypto | undefined {
  // Prefer globalThis; some non-secure contexts expose getRandomValues but not subtle.
  const c =
    (typeof globalThis !== "undefined" ? globalThis.crypto : undefined) ||
    (typeof window !== "undefined" ? window.crypto : undefined);
  return c;
}

function randomSalt(): string {
  const c = webCrypto();
  if (!c?.getRandomValues) {
    throw new Error(
      "Secure random is unavailable in this browser. Use http://127.0.0.1, http://localhost, or HTTPS."
    );
  }
  const bytes = new Uint8Array(16);
  c.getRandomValues(bytes);
  return bytesToBase64url(bytes);
}

/** Pure JS SHA-256 for non-secure contexts where crypto.subtle is missing (e.g. http://LAN-IP). */
function sha256Bytes(data: Uint8Array): Uint8Array {
  const K = new Uint32Array([
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
    0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
    0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
    0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
    0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
    0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
  ]);
  const rotr = (x: number, n: number) => (x >>> n) | (x << (32 - n));
  const bitLen = data.length * 8;
  const withOne = data.length + 1;
  const padLen = (withOne % 64 <= 56 ? 56 : 120) - (withOne % 64);
  const buf = new Uint8Array(withOne + padLen + 8);
  buf.set(data);
  buf[data.length] = 0x80;
  const view = new DataView(buf.buffer);
  // SHA-256 length is 64-bit big-endian; for PIN payloads bitLen fits in low word.
  view.setUint32(buf.length - 4, bitLen >>> 0, false);

  let h0 = 0x6a09e667;
  let h1 = 0xbb67ae85;
  let h2 = 0x3c6ef372;
  let h3 = 0xa54ff53a;
  let h4 = 0x510e527f;
  let h5 = 0x9b05688c;
  let h6 = 0x1f83d9ab;
  let h7 = 0x5be0cd19;
  const w = new Uint32Array(64);

  for (let i = 0; i < buf.length; i += 64) {
    for (let j = 0; j < 16; j++) w[j] = view.getUint32(i + j * 4, false);
    for (let j = 16; j < 64; j++) {
      const s0 = rotr(w[j - 15], 7) ^ rotr(w[j - 15], 18) ^ (w[j - 15] >>> 3);
      const s1 = rotr(w[j - 2], 17) ^ rotr(w[j - 2], 19) ^ (w[j - 2] >>> 10);
      w[j] = (w[j - 16] + s0 + w[j - 7] + s1) >>> 0;
    }
    let a = h0, b = h1, c = h2, d = h3, e = h4, f = h5, g = h6, h = h7;
    for (let j = 0; j < 64; j++) {
      const S1 = rotr(e, 6) ^ rotr(e, 11) ^ rotr(e, 25);
      const ch = (e & f) ^ (~e & g);
      const t1 = (h + S1 + ch + K[j] + w[j]) >>> 0;
      const S0 = rotr(a, 2) ^ rotr(a, 13) ^ rotr(a, 22);
      const maj = (a & b) ^ (a & c) ^ (b & c);
      const t2 = (S0 + maj) >>> 0;
      h = g; g = f; f = e; e = (d + t1) >>> 0;
      d = c; c = b; b = a; a = (t1 + t2) >>> 0;
    }
    h0 = (h0 + a) >>> 0;
    h1 = (h1 + b) >>> 0;
    h2 = (h2 + c) >>> 0;
    h3 = (h3 + d) >>> 0;
    h4 = (h4 + e) >>> 0;
    h5 = (h5 + f) >>> 0;
    h6 = (h6 + g) >>> 0;
    h7 = (h7 + h) >>> 0;
  }

  const out = new Uint8Array(32);
  const outView = new DataView(out.buffer);
  outView.setUint32(0, h0, false);
  outView.setUint32(4, h1, false);
  outView.setUint32(8, h2, false);
  outView.setUint32(12, h3, false);
  outView.setUint32(16, h4, false);
  outView.setUint32(20, h5, false);
  outView.setUint32(24, h6, false);
  outView.setUint32(28, h7, false);
  return out;
}

async function hashPin(pin: string, salt: string): Promise<string> {
  const enc = new TextEncoder();
  // salt|pin — salt is random per enrollment; never store plaintext PIN.
  const data = enc.encode(`${salt}|${pin}`);
  const subtle = webCrypto()?.subtle;
  if (subtle?.digest) {
    const digest = await subtle.digest("SHA-256", data);
    return bytesToBase64url(digest);
  }
  // http://LAN-IP and some WebViews lack SubtleCrypto; pure JS keeps onboarding working.
  return bytesToBase64url(sha256Bytes(data));
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
    webCrypto()?.getRandomValues(challenge);
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
    webCrypto()?.getRandomValues(challenge);
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
