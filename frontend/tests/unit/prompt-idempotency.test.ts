import assert from "node:assert/strict";
import test from "node:test";

import {
  clearPromptRequestId,
  promptRequestFingerprint,
  reservePromptRequestId,
  uploadSelectionFingerprint,
} from "../../src/lib/prompt-idempotency.ts";

class MemoryStorage {
  values = new Map<string, string>();
  getItem(key: string) { return this.values.get(key) ?? null; }
  setItem(key: string, value: string) { this.values.set(key, value); }
  removeItem(key: string) { this.values.delete(key); }
}

test("uncertain prompt retries reuse one persisted request id", () => {
  const storage = new MemoryStorage();
  const fingerprint = promptRequestFingerprint({ text: "hello", attachments: [] });
  const first = reservePromptRequestId("new", fingerprint, storage);
  const retry = reservePromptRequestId("new", fingerprint, storage);

  assert.equal(retry, first);

  clearPromptRequestId("new", first, storage);
  assert.notEqual(reservePromptRequestId("new", fingerprint, storage), first);
});

test("editing a failed prompt receives a new request id", () => {
  const storage = new MemoryStorage();
  const first = reservePromptRequestId(
    "session-1",
    promptRequestFingerprint({ text: "first" }),
    storage,
  );
  const edited = reservePromptRequestId(
    "session-1",
    promptRequestFingerprint({ text: "edited" }),
    storage,
  );

  assert.notEqual(edited, first);
});

test("storage cleanup failures never turn an accepted prompt into a send failure", () => {
  const storage = {
    getItem() {
      throw new Error("storage denied");
    },
    setItem() {
      throw new Error("storage denied");
    },
    removeItem() {
      throw new Error("storage denied");
    },
  };

  assert.doesNotThrow(() => clearPromptRequestId("session", "request", storage));
});

test("the same browser file selection can reuse uploaded attachments on retry", () => {
  const first = uploadSelectionFingerprint([
    { name: "notes.docx", size: 42, type: "application/docx", lastModified: 123 },
  ]);
  const retry = uploadSelectionFingerprint([
    { name: "notes.docx", size: 42, type: "application/docx", lastModified: 123 },
  ]);
  const changed = uploadSelectionFingerprint([
    { name: "notes.docx", size: 43, type: "application/docx", lastModified: 124 },
  ]);

  assert.equal(retry, first);
  assert.notEqual(changed, first);
});
