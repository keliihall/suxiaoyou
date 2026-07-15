import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import { formatFileVersionSize } from "../../src/lib/file-version.ts";

test("file-version API paths encode session, file, and version identifiers", () => {
  const source = readFileSync("src/lib/constants.ts", "utf8");
  assert.match(source, /session_id=\$\{encodeURIComponent\(sessionId\)\}/);
  assert.match(source, /file_path=\$\{encodeURIComponent\(filePath\)\}/);
  assert.match(source, /file-versions\/\$\{encodeURIComponent\(versionId\)\}\/restore/);
  assert.match(source, /\["fileVersions", sessionId, filePath\] as const/);
});

test("file-version sizes are rendered without hiding invalid values", () => {
  assert.equal(formatFileVersionSize(512), "512 B");
  assert.equal(formatFileVersionSize(1536), "1.5 KiB");
  assert.equal(formatFileVersionSize(2 * 1024 * 1024), "2.0 MiB");
  assert.equal(formatFileVersionSize(-1), "—");
});

test("file cards expose durable backend history only for authorized local sessions", () => {
  const card = readFileSync("src/components/parts/file-artifact-card.tsx", "utf8");
  const dialog = readFileSync(
    "src/components/parts/file-version-history-dialog.tsx",
    "utf8",
  );
  assert.match(card, /nativeFileActionsAvailable && sessionId && filePath/);
  assert.match(card, /<FileVersionHistoryDialog/);
  assert.match(dialog, /result\.recovery_version/);
  assert.match(dialog, /fileVersionRestoreConfirm/);
});
