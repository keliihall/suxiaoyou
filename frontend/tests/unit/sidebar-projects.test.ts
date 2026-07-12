import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
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

test("desktop top navigation always exposes new project, including Windows with no projects", () => {
  const topIcons = readFileSync("src/components/layout/window-top-icons.tsx", "utf8");
  const sidebar = readFileSync("src/components/layout/sidebar.tsx", "utf8");

  assert.match(topIcons, /data-testid="window-add-project"/);
  assert.match(topIcons, /browseDirectory\(t\("addProject"\)\)/);
  assert.match(topIcons, /TITLE_BAR_HEIGHT \+ 16/);
  assert.match(sidebar, /!IS_DESKTOP && <ProjectsToolbar variant="primary"/);
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
