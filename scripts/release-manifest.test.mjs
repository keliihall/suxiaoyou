import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { mkdtempSync, mkdirSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import {
  expectedReleaseAssets,
  generateReleaseManifest,
} from "./generate-release-manifest.mjs";
import { verifyReleaseManifest } from "./verify-release-manifest.mjs";

const TAG = "v0.8.0";
const RC_TAG = "v0.8.0-rc.1";
const COMMIT = "a".repeat(40);
const REPOSITORY = "keliihall/suxiaoyou";

function fixture(t, tag = TAG) {
  const root = mkdtempSync(join(tmpdir(), "suxiaoyou-release-manifest-"));
  t.after(() => rmSync(root, { recursive: true, force: true }));
  const assetsDirectory = join(root, "assets");
  mkdirSync(assetsDirectory);
  const checksumRows = [];
  const version = tag.slice(1);
  for (const asset of expectedReleaseAssets(version)) {
    const content = `fixture:${asset.name}`;
    writeFileSync(join(assetsDirectory, asset.name), content);
    const sha256 = createHash("sha256").update(content).digest("hex");
    checksumRows.push(`| \`${asset.name}\` | \`${sha256}\` | 0.0 MiB |`);
  }
  const checksumFile = join(root, "CHECKSUMS.md");
  writeFileSync(
    checksumFile,
    ["## SHA-256 Checksums", "", "| File | SHA-256 | Size |", "|---|---|---|", ...checksumRows].join(
      "\n",
    ),
  );
  const manifestFile = join(root, "release-manifest.json");
  const manifest = generateReleaseManifest({
    assetsDirectory,
    tag,
    commit: COMMIT,
    repository: REPOSITORY,
    checksumFile,
  });
  writeFileSync(manifestFile, `${JSON.stringify(manifest, null, 2)}\n`);
  return { assetsDirectory, checksumFile, manifestFile, manifest };
}

test("generates and verifies a seven-installer manual-download manifest", (t) => {
  const data = fixture(t);
  const verified = verifyReleaseManifest({
    ...data,
    expectedTag: TAG,
    expectedCommit: COMMIT,
    expectedRepository: REPOSITORY,
  });
  assert.equal(verified.updateMode, "manual-download");
  assert.equal(verified.channel, "stable");
  assert.equal(verified.appVersion, "0.8.0");
  assert.equal(verified.assets.length, 7);
  assert.deepEqual(
    verified.assets
      .filter((asset) => asset.platform === "linux" && asset.architecture === "arm64")
      .map(({ format, name }) => ({ format, name })),
    [
      { format: "deb", name: "suxiaoyou-0.8.0-linux-arm64.deb" },
      { format: "rpm", name: "suxiaoyou-0.8.0-linux-aarch64.rpm" },
    ],
  );
  assert.equal(
    verified.checksumUrl,
    "https://github.com/keliihall/suxiaoyou/releases/download/v0.8.0/CHECKSUMS.md",
  );
  assert.ok(verified.assets.every((asset) => asset.downloadUrl.startsWith("https://github.com/")));
  assert.equal("signature" in verified, false);
  assert.equal("updater" in verified, false);
});

test("generates a prerelease manifest with explicit ad-hoc macOS asset names", (t) => {
  const data = fixture(t, RC_TAG);
  const verified = verifyReleaseManifest({
    ...data,
    expectedTag: RC_TAG,
    expectedCommit: COMMIT,
    expectedRepository: REPOSITORY,
  });
  assert.equal(verified.version, "0.8.0-rc.1");
  assert.equal(verified.appVersion, "0.8.0");
  assert.equal(verified.channel, "prerelease");
  const macosAssets = verified.assets.filter((asset) => asset.platform === "macos");
  assert.equal(macosAssets.length, 2);
  assert.ok(macosAssets.every((asset) => asset.name.includes("ADHOC-NOT-NOTARIZED")));
  assert.ok(
    verified.assets
      .filter((asset) => asset.platform !== "macos")
      .every((asset) => !asset.name.includes("ADHOC")),
  );
});

test("rejects a manifest after an installer is tampered", (t) => {
  const data = fixture(t);
  writeFileSync(join(data.assetsDirectory, data.manifest.assets[0].name), "tampered");
  assert.throws(
    () =>
      verifyReleaseManifest({
        ...data,
        expectedTag: TAG,
        expectedCommit: COMMIT,
        expectedRepository: REPOSITORY,
      }),
    /size mismatch|SHA-256 mismatch/,
  );
});

test("rejects automatic-update claims and mismatched release identity", (t) => {
  const data = fixture(t);
  const automatic = { ...data.manifest, updateMode: "automatic" };
  writeFileSync(data.manifestFile, `${JSON.stringify(automatic)}\n`);
  assert.throws(
    () =>
      verifyReleaseManifest({
        ...data,
        expectedTag: TAG,
        expectedCommit: COMMIT,
        expectedRepository: REPOSITORY,
      }),
    /must not claim automatic-update capability/,
  );
  const updaterField = { ...data.manifest, updater: { enabled: true } };
  writeFileSync(data.manifestFile, `${JSON.stringify(updaterField)}\n`);
  assert.throws(
    () =>
      verifyReleaseManifest({
        ...data,
        expectedTag: TAG,
        expectedCommit: COMMIT,
        expectedRepository: REPOSITORY,
      }),
    /fields mismatch/,
  );
  for (const [patch, message] of [
    [{ channel: "prerelease" }, /channel mismatch/],
    [{ appVersion: "9.9.9" }, /app version mismatch/],
  ]) {
    writeFileSync(data.manifestFile, `${JSON.stringify({ ...data.manifest, ...patch })}\n`);
    assert.throws(
      () =>
        verifyReleaseManifest({
          ...data,
          expectedTag: TAG,
          expectedCommit: COMMIT,
          expectedRepository: REPOSITORY,
        }),
      message,
    );
  }
  assert.throws(
    () =>
      generateReleaseManifest({
        ...data,
        tag: "0.8.0",
        commit: COMMIT,
        repository: REPOSITORY,
      }),
    /expected vX\.Y\.Z or vX\.Y\.Z-rc\.N/,
  );
});

test("rejects unsupported prerelease tag shapes", () => {
  for (const tag of [
    "v0.8.0-rc.0",
    "v0.8.0-rc.01",
    "v0.8.0-beta.1",
    "v00.8.0-rc.1",
    "v0.8.0-rc",
  ]) {
    assert.throws(() => expectedReleaseAssets(tag.slice(1)), /Expected format/);
  }
});
