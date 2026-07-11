import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";

import {
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

test("version bump script cannot invoke dependency-upgrade commands", () => {
  const source = readFileSync(join(root, "scripts", "bump-version.mjs"), "utf8");
  assert.doesNotMatch(source, /npm["']?,\s*\[["']install|cargo["']?,\s*\[["']update/);
});
