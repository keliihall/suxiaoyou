import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  clearSessionInputRequestId,
  createSessionInputRequestId,
  removeSessionInput,
  resolveComposerWorkspace,
  reserveSessionInputRequestId,
  sortSessionInputs,
  upsertSessionInput,
} from "../../src/lib/session-inputs.ts";
import type { SessionInputResponse } from "../../src/types/chat.ts";

function input(
  id: string,
  position: number,
  status: SessionInputResponse["status"] = "queued",
): SessionInputResponse {
  return {
    id,
    session_id: "session-1",
    client_request_id: `request-${id}`,
    mode: "queue",
    status,
    position,
    text: `Follow-up ${id}`,
    attachments: [],
  };
}

test("pending session inputs remain ordered and terminal rows are removed", () => {
  assert.deepEqual(
    sortSessionInputs([
      input("third", 3),
      input("done", 1, "consumed"),
      input("first", 1),
      input("second", 2, "applying"),
    ]).map((item) => item.id),
    ["first", "second", "third"],
  );
});

test("idempotent responses upsert by client request id instead of duplicating", () => {
  const first = input("temporary", 1);
  const replay = { ...input("persisted", 1), client_request_id: first.client_request_id };
  const result = upsertSessionInput([first], replay);

  assert.equal(result.length, 1);
  assert.equal(result[0]?.id, "persisted");
  assert.deepEqual(removeSessionInput(result, "persisted"), []);
});

test("session input idempotency keys satisfy the backend length contract", () => {
  const id = createSessionInputRequestId();
  assert.ok(id.length >= 8);
  assert.ok(id.length <= 128);
});

test("uncertain queued inputs retain one opaque id across reloads", () => {
  const values = new Map<string, string>();
  const storage = {
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => values.set(key, value),
    removeItem: (key: string) => values.delete(key),
  };
  const fingerprint = JSON.stringify(["session", "queue", "private text", "/private/path"]);
  const first = reserveSessionInputRequestId(fingerprint, storage);
  const retry = reserveSessionInputRequestId(fingerprint, storage);

  assert.equal(retry, first);
  assert.equal([...values.keys()].some((key) => key.includes("private text")), false);
  assert.equal([...values.keys()].some((key) => key.includes("/private/path")), false);

  clearSessionInputRequestId(fingerprint, first, storage);
  assert.notEqual(reserveSessionInputRequestId(fingerprint, storage), first);
});

test("execution failures are visible instead of disappearing silently", () => {
  const registry = readFileSync("src/lib/session-stream-registry.ts", "utf8");
  const zh = JSON.parse(readFileSync("src/i18n/locales/zh/chat.json", "utf8"));
  const en = JSON.parse(readFileSync("src/i18n/locales/en/chat.json", "utf8"));

  assert.match(registry, /INPUT_FAILED[\s\S]*toast\.error/);
  assert.match(zh.inputExecutionFailedWithReason, /执行失败/);
  assert.match(en.inputExecutionFailedWithReason, /failed/);
});

test("an existing folderless conversation never inherits the last global project", () => {
  assert.equal(
    resolveComposerWorkspace("session-folderless", ".", "/previous/project"),
    null,
  );
  assert.equal(
    resolveComposerWorkspace("session-loading", undefined, "/previous/project"),
    null,
  );
  assert.equal(
    resolveComposerWorkspace("session-project", "/owned/project", "/previous/project"),
    "/owned/project",
  );
  assert.equal(
    resolveComposerWorkspace(undefined, undefined, "/new-chat/default"),
    "/new-chat/default",
  );
});
