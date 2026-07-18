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

test("preserves the attested Office renderer's final signed byte tree", () => {
  assert.match(
    script,
    /OFFICE_RENDERER_ROOT="\$APP_PATH\/Contents\/Resources\/backend\/_internal\/app\/data\/office-renderer"/,
  );
  assert.match(script, /verify_presigned_renderer_code/);
  assert.match(
    script,
    /if \[\[ "\$candidate" == "\$OFFICE_RENDERER_ROOT\/"\* \]\]; then[\s\S]*verify_presigned_renderer_code "\$candidate"[\s\S]*elif \[\[ "\$candidate" == "\$NODE_BINARY"/,
  );
  assert.match(
    script,
    /if \[\[ "\$framework" == "\$OFFICE_RENDERER_ROOT\/"\* \]\]; then\s+verify_presigned_renderer_code "\$framework"/,
  );
  assert.match(script, /codesign --verify --strict --verbose=2 "\$candidate"/);
  assert.match(script, /grep -Fxq "Authority=\$SIGNING_IDENTITY"/);
  assert.match(script, /grep -Fxq "Signature=adhoc"/);
});
