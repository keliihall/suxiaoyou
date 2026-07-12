import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import { getLocalizedContextMenuActionIds } from "../../src/lib/localized-context-menu.ts";

test("uses localized app context menu actions instead of native WebKit actions", () => {
  assert.deepEqual(
    getLocalizedContextMenuActionIds({ isEditable: false, hasSelection: true }),
    ["copy", "selectAll"],
  );
  assert.deepEqual(
    getLocalizedContextMenuActionIds({ isEditable: true, hasSelection: true }),
    ["cut", "copy", "paste", "selectAll"],
  );

  const providerSource = readFileSync("src/components/providers/app-providers.tsx", "utf8");
  const guardSource = readFileSync(
    "src/components/providers/localized-context-menu-guard.tsx",
    "utf8",
  );
  const appContextMenuSource = readFileSync(
    "src/components/ui/context-menu.tsx",
    "utf8",
  );
  assert.match(providerSource, /LocalizedContextMenuGuard/);
  assert.match(guardSource, /\[data-app-context-menu\]/);
  assert.match(appContextMenuSource, /data-app-context-menu/);

  const zhCommon = JSON.parse(readFileSync("src/i18n/locales/zh/common.json", "utf8"));
  assert.equal(zhCommon.contextCopy, "复制");
  assert.equal(zhCommon.contextPaste, "粘贴");
  assert.equal(zhCommon.contextSelectAll, "全选");

  for (const nativeLabel of ["Look Up", "Translate", "Speech", "Inspect Element"]) {
    assert.doesNotMatch(JSON.stringify(zhCommon), new RegExp(nativeLabel));
  }
});

test("session menus expose the local file manager for project and non-project chats", () => {
  const sessionItem = readFileSync("src/components/layout/session-item.tsx", "utf8");
  const sessionList = readFileSync("src/components/layout/session-list.tsx", "utf8");
  const combined = `${sessionItem}\n${sessionList}`;
  const zhCommon = JSON.parse(readFileSync("src/i18n/locales/zh/common.json", "utf8"));
  const enCommon = JSON.parse(readFileSync("src/i18n/locales/en/common.json", "utf8"));

  assert.match(
    sessionItem,
    /const openDirectory = IS_DESKTOP && !isRemoteMode\(\)[\s\S]*\? hasExplicitDirectory[\s\S]*: "\."/,
  );
  assert.match(combined, /API\.FILES\.OPEN_SYSTEM/);
  assert.equal((combined.match(/IS_DESKTOP && !isRemoteMode\(\)/g) ?? []).length, 2);
  assert.match(combined, /usePlatform\(\)/);
  assert.match(combined, /openInFinder/);
  assert.match(combined, /openInExplorer/);
  assert.match(combined, /openInFileManager/);
  assert.equal(zhCommon.openInFinder, "在访达中打开");
  assert.equal(zhCommon.openInExplorer, "在文件资源管理器中打开");
  assert.equal(enCommon.openInExplorer, "Open in File Explorer");
});
