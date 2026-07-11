import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtempSync, mkdirSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";

import * as releaseMetadata from "./release-metadata.mjs";

const { verifyReleaseMetadata } = releaseMetadata;

const VERSION = "0.7.3";
const RELEASE_METADATA_SOURCES = [
  "package.json",
  "package-lock.json top-level version",
  "package-lock.json root entry",
  "frontend/package.json",
  "frontend/package-lock.json top-level version",
  "frontend/package-lock.json root entry",
  "backend/pyproject.toml [project].version",
  "desktop-tauri/src-tauri/tauri.conf.json",
  "desktop-tauri/src-tauri/Cargo.toml [package].version",
  "desktop-tauri/src-tauri/Cargo.lock suxiaoyou-desktop",
  "frontend/src/i18n/locales/en/common.json poweredBy",
  "frontend/src/i18n/locales/zh/common.json poweredBy",
  "THIRD_PARTY_NOTICES.md release graph",
  "release-licenses/SOURCE_AVAILABILITY.md release",
  "release-licenses/RUST-LICENSES.html desktop crate",
];

function writeJson(rootDir, relativePath, value) {
  const filePath = path.join(rootDir, relativePath);
  mkdirSync(path.dirname(filePath), { recursive: true });
  writeFileSync(filePath, `${JSON.stringify(value, null, 2)}\n`);
}

function writeText(rootDir, relativePath, value) {
  const filePath = path.join(rootDir, relativePath);
  mkdirSync(path.dirname(filePath), { recursive: true });
  writeFileSync(filePath, value);
}

function createFixture(overrides = {}) {
  const rootDir = mkdtempSync(path.join(tmpdir(), "suxiaoyou-release-metadata-"));
  const values = {
    root: VERSION,
    rootLockTopLevel: VERSION,
    rootLockRootEntry: VERSION,
    frontend: VERSION,
    frontendLockTopLevel: VERSION,
    frontendLockRootEntry: VERSION,
    backend: VERSION,
    tauri: VERSION,
    cargo: VERSION,
    cargoLock: VERSION,
    enPoweredBy: `苏小有 v${VERSION}`,
    zhPoweredBy: `苏小有 v${VERSION}`,
    thirdParty: VERSION,
    sourceAvailability: VERSION,
    rustLicense: VERSION,
    ...overrides,
  };

  writeJson(rootDir, "package.json", { name: "suxiaoyou", version: values.root });
  writeJson(rootDir, "package-lock.json", {
    name: "suxiaoyou",
    version: values.rootLockTopLevel,
    lockfileVersion: 3,
    packages: { "": { name: "suxiaoyou", version: values.rootLockRootEntry } },
  });
  writeJson(rootDir, "frontend/package.json", {
    name: "suxiaoyou-frontend",
    version: values.frontend,
  });
  writeJson(rootDir, "frontend/package-lock.json", {
    name: "suxiaoyou-frontend",
    version: values.frontendLockTopLevel,
    lockfileVersion: 3,
    packages: {
      "": { name: "suxiaoyou-frontend", version: values.frontendLockRootEntry },
    },
  });
  writeText(
    rootDir,
    "backend/pyproject.toml",
    `[project]\nname = "suxiaoyou"\nversion = "${values.backend}"\n`,
  );
  writeJson(rootDir, "desktop-tauri/src-tauri/tauri.conf.json", {
    productName: "苏小有",
    version: values.tauri,
  });
  writeText(
    rootDir,
    "desktop-tauri/src-tauri/Cargo.toml",
    `[package]\nname = "suxiaoyou-desktop"\nversion = "${values.cargo}"\n`,
  );
  writeText(
    rootDir,
    "desktop-tauri/src-tauri/Cargo.lock",
    `version = 4\n\n[[package]]\nname = "unrelated"\nversion = "9.9.9"\n\n[[package]]\nname = "suxiaoyou-desktop"\nversion = "${values.cargoLock}"\n`,
  );
  writeJson(rootDir, "frontend/src/i18n/locales/en/common.json", {
    poweredBy: values.enPoweredBy,
  });
  writeJson(rootDir, "frontend/src/i18n/locales/zh/common.json", {
    poweredBy: values.zhPoweredBy,
  });
  writeText(
    rootDir,
    "THIRD_PARTY_NOTICES.md",
    `The v${values.thirdParty} production graphs include locked dependencies.\n`,
  );
  writeText(
    rootDir,
    "release-licenses/SOURCE_AVAILABILITY.md",
    `MPL-2.0 components included in 苏小有 v${values.sourceAvailability}.\n`,
  );
  writeText(
    rootDir,
    "release-licenses/RUST-LICENSES.html",
    `<a>suxiaoyou dependency</a><a href="#">suxiaoyou-desktop ${values.rustLicense}</a>\n`,
  );

  return rootDir;
}

test("accepts a completely consistent release fixture", (t) => {
  const rootDir = createFixture();
  t.after(() => rmSync(rootDir, { recursive: true, force: true }));

  assert.doesNotThrow(() => verifyReleaseMetadata(rootDir, VERSION));
});

test("keeps application metadata on stable X.Y.Z versions", () => {
  assert.doesNotThrow(() => releaseMetadata.assertReleaseVersion("0.8.0"));
  assert.throws(
    () => releaseMetadata.assertReleaseVersion("0.8.0-rc.1"),
    /Expected format: X\.Y\.Z/,
  );
});

test("reports every mismatched release consumer in one error", (t) => {
  const rootDir = createFixture({
    root: "1.0.0",
    rootLockTopLevel: "2.0.0",
    rootLockRootEntry: "3.0.0",
    frontend: "4.0.0",
    frontendLockTopLevel: "5.0.0",
    frontendLockRootEntry: "6.0.0",
    backend: "7.0.0",
    tauri: "8.0.0",
    cargo: "9.0.0",
    cargoLock: "10.0.0",
    enPoweredBy: "Wrong product v0.7.3",
    zhPoweredBy: "苏小有 v9.0.0",
    thirdParty: "11.0.0",
    sourceAvailability: "12.0.0",
    rustLicense: "13.0.0",
  });
  t.after(() => rmSync(rootDir, { recursive: true, force: true }));

  assert.throws(
    () => verifyReleaseMetadata(rootDir, VERSION),
    (error) => {
      const diagnostics = error.message.split("\n").slice(1);
      assert.equal(diagnostics.length, RELEASE_METADATA_SOURCES.length);
      assert.deepEqual(
        diagnostics.map((line) => line.slice(2, line.indexOf(":"))),
        RELEASE_METADATA_SOURCES,
      );
      return true;
    },
  );
});

test("detects stale lockfile top-level versions when root entries match", (t) => {
  const rootDir = createFixture({
    rootLockTopLevel: "1.0.0",
    frontendLockTopLevel: "2.0.0",
  });
  t.after(() => rmSync(rootDir, { recursive: true, force: true }));

  assert.throws(
    () => verifyReleaseMetadata(rootDir, VERSION),
    (error) => {
      const diagnostics = error.message.split("\n").slice(1);
      assert.deepEqual(
        diagnostics.map((line) => line.slice(2, line.indexOf(":"))),
        [
          "package-lock.json top-level version",
          "frontend/package-lock.json top-level version",
        ],
      );
      return true;
    },
  );
});

test("detects stale lockfile root entries when top-level versions match", (t) => {
  const rootDir = createFixture({
    rootLockRootEntry: "1.0.0",
    frontendLockRootEntry: "2.0.0",
  });
  t.after(() => rmSync(rootDir, { recursive: true, force: true }));

  assert.throws(
    () => verifyReleaseMetadata(rootDir, VERSION),
    (error) => {
      const diagnostics = error.message.split("\n").slice(1);
      assert.deepEqual(
        diagnostics.map((line) => line.slice(2, line.indexOf(":"))),
        ["package-lock.json root entry", "frontend/package-lock.json root entry"],
      );
      return true;
    },
  );
});

test("rejects malformed expected release versions", (t) => {
  const rootDir = createFixture();
  t.after(() => rmSync(rootDir, { recursive: true, force: true }));

  for (const version of ["v0.7.3", "0.7", "0.7.3-beta", "release-0.7.3"]) {
    assert.throws(
      () => verifyReleaseMetadata(rootDir, version),
      /Invalid expected version.*X\.Y\.Z/,
    );
  }
});

test("updates keys only inside the requested TOML section", () => {
  const original = `[workspace.package]\nversion = "9.9.9"\ndescription = "workspace defaults"\n\n[package]\nname = "suxiaoyou-desktop"\nversion = "0.7.2"\ndescription = "old desktop description"\n`;

  const updated = releaseMetadata.replaceTomlSectionValues(
    original,
    "package",
    { version: VERSION, description: "new desktop description" },
    "Cargo.toml",
  );

  assert.match(
    updated,
    /\[workspace\.package\]\nversion = "9\.9\.9"\ndescription = "workspace defaults"/,
  );
  assert.match(
    updated,
    /\[package\]\nname = "suxiaoyou-desktop"\nversion = "0\.7\.3"\ndescription = "new desktop description"/,
  );
});

test("rejects a missing section key instead of changing a later section", () => {
  const original = `[project]\nname = "suxiaoyou"\n\n[tool.example]\nversion = "9.9.9"\n`;

  assert.throws(
    () =>
      releaseMetadata.replaceTomlSectionValues(
        original,
        "project",
        { version: VERSION },
        "backend/pyproject.toml",
      ),
    /backend\/pyproject\.toml is missing \[project\]\.version/,
  );
  assert.match(original, /\[tool\.example\]\nversion = "9\.9\.9"/);
});

test("combines structural errors with all readable value mismatches", (t) => {
  const rootDir = createFixture({ frontend: "8.0.0" });
  t.after(() => rmSync(rootDir, { recursive: true, force: true }));
  writeText(rootDir, "backend/pyproject.toml", `[project]\nname = "suxiaoyou"\n`);
  writeText(
    rootDir,
    "desktop-tauri/src-tauri/Cargo.lock",
    `version = 4\n\n[[package]]\nname = "unrelated"\nversion = "9.9.9"\n`,
  );

  assert.throws(
    () => verifyReleaseMetadata(rootDir, VERSION),
    (error) => {
      assert.ok(error.message.includes("- frontend/package.json:"));
      assert.ok(error.message.includes("- backend/pyproject.toml [project].version:"));
      assert.ok(
        error.message.includes("- desktop-tauri/src-tauri/Cargo.lock suxiaoyou-desktop:"),
      );
      return true;
    },
  );
});

test("runs the verifier CLI when invoked through a symlink", (t) => {
  const rootDir = createFixture();
  t.after(() => rmSync(rootDir, { recursive: true, force: true }));
  const cliPath = path.join(rootDir, "verify-release.mjs");
  symlinkSync(path.resolve("scripts/release-metadata.mjs"), cliPath);

  const result = spawnSync(process.execPath, [cliPath, VERSION], {
    cwd: rootDir,
    encoding: "utf8",
  });

  assert.equal(result.status, 0, result.stderr);
  assert.equal(result.stdout, `Release metadata verified at ${VERSION}\n`);
});
