import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import { getFileArtifactActionIds } from "../../src/lib/file-artifact-actions.ts";

test("local desktop file cards expose host actions through a shared policy", () => {
  assert.deepEqual(getFileArtifactActionIds(true), [
    "preview",
    "openDefault",
    "reveal",
    "copyPath",
    "saveCopy",
  ]);
});

test("remote and browser file cards never expose host paths or launch actions", () => {
  assert.deepEqual(getFileArtifactActionIds(false), ["preview", "saveCopy"]);
});

test("file cards use real sibling buttons for preview and menu actions", () => {
  const source = readFileSync(
    "src/components/parts/file-artifact-card.tsx",
    "utf8",
  );

  assert.match(source, /<ContextMenu>/);
  assert.match(source, /<DropdownMenu>/);
  assert.match(source, /<ContextMenuTrigger asChild>/);
  assert.match(source, /<DropdownMenuTrigger asChild>/);
  assert.doesNotMatch(source, /role="button"/);

  const zh = JSON.parse(readFileSync("src/i18n/locales/zh/chat.json", "utf8"));
  const en = JSON.parse(readFileSync("src/i18n/locales/en/chat.json", "utf8"));
  assert.equal(zh.openWith, "打开方式");
  assert.equal(zh.revealInExplorer, "在文件资源管理器中显示");
  assert.equal(en.openWithDefaultApp, "Open with default app");
});
