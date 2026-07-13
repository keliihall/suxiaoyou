import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const enChat = JSON.parse(readFileSync("src/i18n/locales/en/chat.json", "utf8"));
const zhChat = JSON.parse(readFileSync("src/i18n/locales/zh/chat.json", "utf8"));
const enSettings = JSON.parse(
  readFileSync("src/i18n/locales/en/settings.json", "utf8"),
);
const zhSettings = JSON.parse(
  readFileSync("src/i18n/locales/zh/settings.json", "utf8"),
);
const permissionDialog = readFileSync(
  "src/components/interactive/permission-dialog.tsx",
  "utf8",
);
const rapidMlxPanel = readFileSync(
  "src/components/settings/rapid-mlx-panel.tsx",
  "utf8",
);

test("permission notification brand and copy follow the active language", () => {
  assert.equal(enChat.permissionNotificationTitle, "suyo — Permission Required");
  assert.equal(zhChat.permissionNotificationTitle, "苏小有 — 需要权限");
  assert.match(permissionDialog, /t\("permissionNotificationTitle"\)/);
  assert.match(permissionDialog, /t\("permissionNotificationBody"/);
  assert.doesNotMatch(permissionDialog, /new Notification\("suyo/);
});

test("Rapid-MLX uninstall dialog uses localized app-name copy", () => {
  assert.equal(enSettings.rapidMlxUninstallTitle, "Uninstall Rapid-MLX from suyo?");
  assert.equal(zhSettings.rapidMlxUninstallTitle, "从苏小有中卸载 Rapid-MLX？");
  assert.equal(enSettings.rapidMlxSettingsCleared, "suyo settings cleared");
  assert.equal(zhSettings.rapidMlxSettingsCleared, "已清除苏小有设置");
  assert.match(rapidMlxPanel, /t\("rapidMlxUninstallTitle"\)/);
  assert.match(rapidMlxPanel, /t\("rapidMlxUninstallDescription"\)/);
  assert.doesNotMatch(rapidMlxPanel, /Uninstall Rapid-MLX from suyo/);
  assert.doesNotMatch(rapidMlxPanel, /suyo settings cleared/);
});
