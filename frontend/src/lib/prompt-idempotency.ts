type StorageLike = Pick<Storage, "getItem" | "setItem" | "removeItem">;

interface PendingPromptRecord {
  fingerprint: string;
  requestId: string;
}

const STORAGE_PREFIX = "suxiaoyou:pending-prompt:v1:";

function requestId(): string {
  if (typeof globalThis.crypto?.randomUUID === "function") {
    return globalThis.crypto.randomUUID();
  }
  return `prompt-${Date.now()}-${Math.random().toString(36).slice(2, 12)}`;
}

/** A compact deterministic fingerprint; raw prompt text is never persisted. */
export function promptRequestFingerprint(payload: unknown): string {
  const input = JSON.stringify(payload);
  let first = 0x811c9dc5;
  let second = 0x9e3779b9;
  for (let index = 0; index < input.length; index += 1) {
    const code = input.charCodeAt(index);
    first = Math.imul(first ^ code, 0x01000193);
    second = Math.imul(second ^ code, 0x85ebca6b);
  }
  return `${(first >>> 0).toString(16).padStart(8, "0")}${(second >>> 0).toString(16).padStart(8, "0")}`;
}

/** Stable identity for a browser File selection across a prompt retry. */
export function uploadSelectionFingerprint(
  files: Array<{
    name: string;
    size: number;
    type: string;
    lastModified: number;
  }>,
): string {
  return promptRequestFingerprint(
    files.map((file) => [
      file.name,
      file.size,
      file.type,
      file.lastModified,
    ]),
  );
}

export function reservePromptRequestId(
  scope: string,
  fingerprint: string,
  storage: StorageLike | null = typeof localStorage === "undefined" ? null : localStorage,
): string {
  const key = `${STORAGE_PREFIX}${scope}`;
  if (storage) {
    try {
      const raw = storage.getItem(key);
      const existing = raw ? JSON.parse(raw) as PendingPromptRecord : null;
      if (
        existing?.fingerprint === fingerprint
        && typeof existing.requestId === "string"
      ) {
        return existing.requestId;
      }
    } catch {
      // Corrupt or unavailable storage is non-fatal; replace it below.
    }
  }

  const next = requestId();
  try {
    storage?.setItem(key, JSON.stringify({ fingerprint, requestId: next }));
  } catch {
    // The in-flight HTTP retry still reuses ``next`` even if persistence is
    // unavailable (private mode, quota, or a restricted webview).
  }
  return next;
}

export function clearPromptRequestId(
  scope: string,
  expectedRequestId: string,
  storage: StorageLike | null = typeof localStorage === "undefined" ? null : localStorage,
): void {
  if (!storage) return;
  const key = `${STORAGE_PREFIX}${scope}`;
  try {
    const raw = storage.getItem(key);
    const existing = raw ? JSON.parse(raw) as PendingPromptRecord : null;
    if (existing?.requestId === expectedRequestId) storage.removeItem(key);
  } catch {
    // Restricted/private storage may throw on both reads and writes. A
    // successful backend response must not be turned into a visible send
    // failure merely because best-effort cleanup is unavailable.
    try {
      storage.removeItem(key);
    } catch {
      // Best effort only.
    }
  }
}
