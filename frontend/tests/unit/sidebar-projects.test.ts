import assert from "node:assert/strict";
import test from "node:test";

import {
  directoryLabelOf,
  groupSessionsByWorkspace,
  normalizeDirectory,
} from "../../src/lib/utils.ts";

test("workspace normalization preserves POSIX and Windows roots", () => {
  assert.equal(normalizeDirectory("/"), "/");
  assert.equal(normalizeDirectory("////"), "/");
  assert.equal(normalizeDirectory("C:\\"), "C:/");
  assert.equal(normalizeDirectory("C:///"), "C:/");
  assert.equal(normalizeDirectory("/Users/example///"), "/Users/example");
  assert.equal(normalizeDirectory("C:\\Users\\example\\"), "C:/Users/example");
  assert.equal(directoryLabelOf("/"), "/");
  assert.equal(directoryLabelOf("C:\\"), "C:/");
});

test("root workspaces remain projects instead of collapsing into unscoped chats", () => {
  const grouped = groupSessionsByWorkspace([
    { id: "posix", directory: "/" },
    { id: "windows", directory: "C:\\" },
    { id: "chat", directory: "." },
  ]);

  assert.deepEqual(
    grouped.projects.map((project) => project.directory),
    ["/", "C:/"],
  );
  assert.deepEqual(grouped.chats.map((session) => session.id), ["chat"]);
});
