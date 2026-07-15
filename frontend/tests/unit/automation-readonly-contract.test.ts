import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

test("automation UI states the unattended read-only capability ceiling", () => {
  const zh = JSON.parse(readFileSync("src/i18n/locales/zh/automations.json", "utf8"));
  const en = JSON.parse(readFileSync("src/i18n/locales/en/automations.json", "utf8"));
  const content = readFileSync("src/app/(main)/automations/content.tsx", "utf8");
  const dialogs = readFileSync(
    "src/app/(main)/automations/automation-dialogs.tsx",
    "utf8",
  );

  assert.match(zh.readOnlyTitle, /只读/);
  assert.match(zh.readOnlyDescription, /不能修改文件/);
  assert.match(en.readOnlyTitle, /read-only/i);
  assert.doesNotMatch(zh.workspaceNone, /全局|不限制/);
  assert.doesNotMatch(en.workspaceNone, /unrestricted|global/i);
  assert.match(content, /<AutomationReadOnlyNotice/);
  assert.match(dialogs, /<AutomationReadOnlyNotice compact/);
});

test("automation history renders persisted failure reasons", () => {
  const source = readFileSync("src/app/(main)/automations/shared-ui.tsx", "utf8");
  assert.match(source, /run\.error_message/);
  assert.match(source, /automation-run-error/);
  assert.match(source, /runErrorDetail/);
});
