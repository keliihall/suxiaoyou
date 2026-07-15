import assert from "node:assert/strict";
import { readFileSync, statSync } from "node:fs";
import { test } from "node:test";

const path = new URL("./sign-macos-app.sh", import.meta.url);
const script = readFileSync(path, "utf8");

test("local macOS signing is inside-out with a stable backend identifier", () => {
  assert.equal(statSync(path).mode & 0o111, 0o111);
  assert.match(script, /paths\.sort\(key=lambda path: \(path\.count\(os\.sep\), path\), reverse=True\)/);
  assert.match(script, /BACKEND_IDENTIFIER="com\.suxiaoyou\.backend"/);
  assert.match(script, /--identifier "\$BACKEND_IDENTIFIER"/);
  assert.match(script, /--entitlements "\$BACKEND_ADHOC_ENTITLEMENTS"/);
  assert.match(script, /--entitlements "\$NODE_ENTITLEMENTS"/);
  assert.match(script, /codesign --verify --deep --strict --verbose=2 "\$APP_PATH"/);
});

test("local macOS signing does not weaken Node or backend release security", () => {
  assert.doesNotMatch(script, /codesign --force[^\n]*--deep/);
  assert.match(script, /Developer ID backend must not disable library validation/);
  for (const forbidden of [
    "com.apple.security.get-task-allow",
    "com.apple.security.cs.allow-dyld-environment-variables",
    "com.apple.security.cs.disable-executable-page-protection",
    "com.apple.security.cs.disable-library-validation",
  ]) {
    assert.match(script, new RegExp(forbidden.replaceAll(".", "\\.")));
  }
});
