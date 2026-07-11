import type { SessionInputResponse } from "@/types/chat";
import { promptRequestFingerprint } from "./prompt-idempotency.ts";

const TERMINAL_INPUT_STATUSES = new Set(["consumed", "failed", "cancelled"]);
const PENDING_INPUT_STORAGE_PREFIX = "suxiaoyou:pending-session-input:v1:";
type StorageLike = Pick<Storage, "getItem" | "setItem" | "removeItem">;

/**
 * Existing conversations own their workspace. A folderless session must not
 * inherit the last project selected globally; only a not-yet-created chat may
 * use that global default.
 */
export function resolveComposerWorkspace(
  sessionId: string | undefined,
  directory: string | null | undefined,
  globalWorkspace: string | null | undefined,
): string | null {
  if (sessionId) {
    return directory && directory !== "." ? directory : null;
  }
  return directory && directory !== "."
    ? directory
    : globalWorkspace ?? null;
}

export function createSessionInputRequestId(): string {
  if (typeof globalThis.crypto?.randomUUID === "function") {
    return globalThis.crypto.randomUUID();
  }
  return `input-${Date.now()}-${Math.random().toString(36).slice(2, 12)}`;
}

/**
 * Persist an uncertain queue admission across reloads without storing the
 * user's text or local file paths. The fingerprint is hashed into the key and
 * only the opaque request id is retained.
 */
export function reserveSessionInputRequestId(
  fingerprint: string,
  storage: StorageLike | null = typeof localStorage === "undefined" ? null : localStorage,
): string {
  const key = `${PENDING_INPUT_STORAGE_PREFIX}${promptRequestFingerprint(fingerprint)}`;
  if (storage) {
    try {
      const existing = storage.getItem(key);
      if (existing) return existing;
    } catch {
      // Fall through to an in-memory id when browser storage is unavailable.
    }
  }

  const id = createSessionInputRequestId();
  try {
    storage?.setItem(key, id);
  } catch {
    // Best effort; the immediate HTTP retry still reuses the request body.
  }
  return id;
}

export function clearSessionInputRequestId(
  fingerprint: string,
  expectedRequestId: string,
  storage: StorageLike | null = typeof localStorage === "undefined" ? null : localStorage,
): void {
  if (!storage) return;
  const key = `${PENDING_INPUT_STORAGE_PREFIX}${promptRequestFingerprint(fingerprint)}`;
  try {
    if (storage.getItem(key) === expectedRequestId) storage.removeItem(key);
  } catch {
    try {
      storage.removeItem(key);
    } catch {
      // Best effort only.
    }
  }
}

export function sortSessionInputs(items: SessionInputResponse[]): SessionInputResponse[] {
  return [...items]
    .filter((item) => !TERMINAL_INPUT_STATUSES.has(item.status))
    .sort((a, b) => a.position - b.position || a.id.localeCompare(b.id));
}

export function upsertSessionInput(
  items: SessionInputResponse[] | undefined,
  incoming: SessionInputResponse,
): SessionInputResponse[] {
  const current = items ?? [];
  const index = current.findIndex(
    (item) => item.id === incoming.id || item.client_request_id === incoming.client_request_id,
  );
  const next = index < 0
    ? [...current, incoming]
    : current.map((item, itemIndex) => itemIndex === index ? incoming : item);
  return sortSessionInputs(next);
}

export function removeSessionInput(
  items: SessionInputResponse[] | undefined,
  inputId: string,
): SessionInputResponse[] {
  return (items ?? []).filter((item) => item.id !== inputId);
}
