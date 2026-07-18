import assert from "node:assert/strict";
import {
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";

import {
  prepareEmbeddedReleaseVersionUpdates,
  replacePythonFinalString,
  replaceRequiredReleaseReference,
  updateCargoLockVersion,
  updateNpmLockVersion,
} from "./bump-version.mjs";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");

test("updates only npm project version fields", () => {
  const lock = {
    name: "suxiaoyou",
    version: "0.7.2",
    lockfileVersion: 3,
    packages: {
      "": { name: "suxiaoyou", version: "0.7.2", dependencies: { next: "15.5.18" } },
      "node_modules/next": { version: "15.5.18" },
    },
    dependencies: { next: { version: "15.5.18" } },
  };

  const updated = updateNpmLockVersion(lock, "0.7.3", "package-lock.json");

  assert.equal(updated.version, "0.7.3");
  assert.equal(updated.packages[""].version, "0.7.3");
  assert.equal(updated.packages["node_modules/next"].version, "15.5.18");
  assert.equal(updated.dependencies.next.version, "15.5.18");
  assert.equal(lock.version, "0.7.2", "helper must not mutate the caller's object");
});

test("updates only the desktop package in Cargo.lock", () => {
  const lock = `version = 4

[[package]]
name = "dependency"
version = "9.9.9"

[[package]]
name = "suxiaoyou-desktop"
version = "0.7.2"
dependencies = [
 "dependency",
]
`;

  const updated = updateCargoLockVersion(lock, "0.7.3");

  assert.match(updated, /name = "dependency"\nversion = "9\.9\.9"/);
  assert.match(updated, /name = "suxiaoyou-desktop"\nversion = "0\.7\.3"/);
  assert.doesNotMatch(updated, /name = "suxiaoyou-desktop"\nversion = "0\.7\.2"/);
});

test("updates the backend runtime app version exactly", () => {
  const source = `from typing import Final\n\nAPP_VERSION: Final = "1.0.0"\nOTHER_VERSION: Final = "9.9.9"\n`;
  const updated = replacePythonFinalString(
    source,
    "APP_VERSION",
    "1.1.0",
    "backend/app/version.py",
  );
  assert.match(updated, /APP_VERSION: Final = "1\.1\.0"/);
  assert.match(updated, /OTHER_VERSION: Final = "9\.9\.9"/);
});

test("version bump script cannot invoke dependency-upgrade commands", () => {
  const source = readFileSync(join(root, "scripts", "bump-version.mjs"), "utf8");
  assert.doesNotMatch(source, /npm["']?,\s*\[["']install|cargo["']?,\s*\[["']update/);
});

test("updates embedded release references without changing dependency versions", (t) => {
  const directory = mkdtempSync(join(tmpdir(), "suxiaoyou-version-references-"));
  t.after(() => rmSync(directory, { recursive: true, force: true }));
  mkdirSync(join(directory, "release-licenses"));
  writeFileSync(
    join(directory, "THIRD_PARTY_NOTICES.md"),
    "The v0.7.3 production graphs include dependency 0.7.3.\n",
  );
  writeFileSync(
    join(directory, "release-licenses", "SOURCE_AVAILABILITY.md"),
    "MPL-2.0 components included in 苏小有 v0.7.3. dependency 0.7.3\n",
  );
  writeFileSync(
    join(directory, "release-licenses", "RUST-LICENSES.html"),
    ">suxiaoyou-desktop 0.7.3</a><a>rand 0.7.3</a>\n",
  );

  const updates = prepareEmbeddedReleaseVersionUpdates(directory, "0.7.3", "0.8.0");
  assert.equal(updates.length, 3);
  const combined = updates.map(({ updated }) => updated).join("\n");
  assert.match(combined, /v0\.8\.0 production graphs/);
  assert.match(combined, /苏小有 v0\.8\.0/);
  assert.match(combined, />suxiaoyou-desktop 0\.8\.0<\/a>/);
  assert.match(combined, /dependency 0\.7\.3/);
  assert.match(combined, /rand 0\.7\.3/);
});

test("rejects missing or ambiguous embedded release references", () => {
  assert.throws(
    () => replaceRequiredReleaseReference("none", "v0.7.3", "v0.8.0", "fixture"),
    /found 0/,
  );
  assert.throws(
    () =>
      replaceRequiredReleaseReference(
        "v0.7.3 and v0.7.3",
        "v0.7.3",
        "v0.8.0",
        "fixture",
      ),
    /found 2/,
  );
});
