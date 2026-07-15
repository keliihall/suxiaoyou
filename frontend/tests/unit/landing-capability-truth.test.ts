import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

test("landing starters stay inside the current shipped capability boundary", () => {
  const source = readFileSync("src/components/chat/landing.tsx", "utf8");
  const zh = JSON.parse(readFileSync("src/i18n/locales/zh/chat.json", "utf8"));
  const en = JSON.parse(readFileSync("src/i18n/locales/en/chat.json", "utf8"));

  assert.doesNotMatch(source, /starterRenamePhotos|starterExtractPdfTables/);
  assert.doesNotMatch(zh.starterOrganizeBillsPrompt, /下载文件夹|整个电脑/);
  assert.doesNotMatch(en.starterOrganizeBillsPrompt, /Downloads folder|entire computer/i);
  assert.match(zh.starterOrganizeBillsPrompt, /当前工作区/);
  assert.match(en.starterOrganizeBillsPrompt, /current workspace/i);
  assert.equal(zh.workspaceNone, "未选择工作区");
  assert.equal(en.workspaceNone, "No workspace selected");
  assert.doesNotMatch(zh.capDataExtractionDesc, /提取表格/);
  assert.doesNotMatch(en.capDataExtractionDesc, /pull tables/i);
});
