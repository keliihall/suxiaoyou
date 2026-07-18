import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import {
  mkdtempSync,
  mkdirSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";

import {
  expectedReleaseAssets,
  generateReleaseManifest,
} from "./generate-release-manifest.mjs";
import { verifyReleaseManifest } from "./verify-release-manifest.mjs";

const TAG = "v0.8.0";
const RC_TAG = "v0.8.0-rc.1";
const UNSIGNED_DEGRADED_TAG = "v1.1.0";
const COMMIT = "a".repeat(40);
const REPOSITORY = "keliihall/suxiaoyou";

function fixture(t, tag = TAG, releaseProfile) {
  const root = mkdtempSync(join(tmpdir(), "suxiaoyou-release-manifest-"));
  t.after(() => rmSync(root, { recursive: true, force: true }));
  const assetsDirectory = join(root, "assets");
  mkdirSync(assetsDirectory);
  const checksumRows = [];
  const version = tag.slice(1);
  for (const asset of expectedReleaseAssets(version, releaseProfile)) {
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
    releaseProfile,
  });
  writeFileSync(manifestFile, `${JSON.stringify(manifest, null, 2)}\n`);
  return {
    root,
    assetsDirectory,
    checksumFile,
    manifestFile,
    manifest,
    releaseProfile,
  };
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
  assert.equal(verified.schemaVersion, 2);
  assert.equal(verified.channel, "stable");
  assert.equal(verified.appVersion, "0.8.0");
  assert.equal(verified.assets.length, 7);
  assert.deepEqual(
    verified.assets
      .filter((asset) => asset.platform === "linux" && asset.architecture === "arm64")
      .map(({ format, name }) => ({ format, name })),
    [
      { format: "deb", name: "suyo-0.8.0-linux-arm64.deb" },
      { format: "rpm", name: "suyo-0.8.0-linux-aarch64.rpm" },
    ],
  );
  assert.equal(
    verified.checksumUrl,
    "https://github.com/keliihall/suxiaoyou/releases/download/v0.8.0/CHECKSUMS.md",
  );
  assert.ok(verified.assets.every((asset) => asset.downloadUrl.startsWith("https://github.com/")));
  assert.equal("signature" in verified, false);
  assert.equal("updater" in verified, false);
  assert.equal("releaseProfile" in verified, false);
  assert.equal("officialReleaseEligible" in verified, false);
  assert.equal("latestEligible" in verified, false);
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
  assert.equal(verified.schemaVersion, 2);
  assert.equal(verified.appVersion, "0.8.0");
  assert.equal(verified.channel, "prerelease");
  assert.equal("releaseProfile" in verified, false);
  const macosAssets = verified.assets.filter((asset) => asset.platform === "macos");
  assert.equal(macosAssets.length, 2);
  assert.ok(macosAssets.every((asset) => asset.name.includes("ADHOC-NOT-NOTARIZED")));
  assert.ok(
    verified.assets
      .filter((asset) => asset.platform !== "macos")
      .every((asset) => !asset.name.includes("ADHOC")),
  );
});

test("generates and verifies the explicit v1.1 unsigned-degraded profile", (t) => {
  const data = fixture(t, UNSIGNED_DEGRADED_TAG, "unsigned-degraded");
  const verified = verifyReleaseManifest({
    ...data,
    expectedTag: UNSIGNED_DEGRADED_TAG,
    expectedCommit: COMMIT,
    expectedRepository: REPOSITORY,
  });

  assert.equal(verified.schemaVersion, 3);
  assert.equal(verified.releaseProfile, "unsigned-degraded");
  assert.equal(verified.channel, "stable");
  assert.equal(verified.publicationChannel, "prerelease");
  assert.equal(verified.officialReleaseEligible, false);
  assert.equal(verified.latestEligible, false);
  assert.deepEqual(verified.trust, {
    windowsAuthenticodeSigned: false,
    macosAppSignature: "adhoc",
    macosDeveloperIdSigned: false,
    macosDmgSigned: false,
    macosNotarized: false,
    macosStapled: false,
    linuxDebSigned: false,
    linuxRpmSigned: false,
    linuxRepositorySigned: false,
  });
  assert.deepEqual(verified.capabilities, {
    v11Gates: "released",
    officeRenderer: "absent",
    officeAuthoring: "unavailable",
    integrations: "not-run",
  });
  assert.deepEqual(
    verified.assets.map((asset) => asset.name),
    [
      "suyo-1.1.0-windows-x64-setup-UNSIGNED-DEGRADED.exe",
      "suyo-1.1.0-macos-aarch64-UNSIGNED-DEGRADED.dmg",
      "suyo-1.1.0-macos-x64-UNSIGNED-DEGRADED.dmg",
      "suyo-1.1.0-linux-amd64-UNSIGNED-DEGRADED.deb",
      "suyo-1.1.0-linux-x86_64-UNSIGNED-DEGRADED.rpm",
      "suyo-1.1.0-linux-arm64-UNSIGNED-DEGRADED.deb",
      "suyo-1.1.0-linux-aarch64-UNSIGNED-DEGRADED.rpm",
    ],
  );
});

test("unsigned-degraded schema cannot claim official trust or capabilities", (t) => {
  const data = fixture(t, UNSIGNED_DEGRADED_TAG, "unsigned-degraded");
  const mutations = [
    {
      ...data.manifest,
      officialReleaseEligible: true,
    },
    {
      ...data.manifest,
      publicationChannel: "stable",
    },
    {
      ...data.manifest,
      trust: { ...data.manifest.trust, macosNotarized: true },
    },
    {
      ...data.manifest,
      trust: { ...data.manifest.trust, undocumentedTrustClaim: false },
    },
    {
      ...data.manifest,
      capabilities: {
        ...data.manifest.capabilities,
        officeRenderer: "present",
      },
    },
  ];
  for (const mutation of mutations) {
    writeFileSync(data.manifestFile, `${JSON.stringify(mutation)}\n`);
    assert.throws(
      () =>
        verifyReleaseManifest({
          ...data,
          expectedTag: UNSIGNED_DEGRADED_TAG,
          expectedCommit: COMMIT,
          expectedRepository: REPOSITORY,
        }),
      /mismatch|fields mismatch/u,
    );
  }
});

test("manifest CLIs accept unsigned-degraded as the final profile argument", (t) => {
  const data = fixture(t, UNSIGNED_DEGRADED_TAG, "unsigned-degraded");
  const manifestFile = join(data.root, "release-manifest-cli.json");
  const generator = spawnSync(
    process.execPath,
    [
      fileURLToPath(new URL("./generate-release-manifest.mjs", import.meta.url)),
      data.assetsDirectory,
      UNSIGNED_DEGRADED_TAG,
      COMMIT,
      REPOSITORY,
      data.checksumFile,
      manifestFile,
      "unsigned-degraded",
    ],
    { encoding: "utf8" },
  );
  assert.equal(generator.status, 0, generator.stderr);
  assert.equal(
    JSON.parse(readFileSync(manifestFile, "utf8")).releaseProfile,
    "unsigned-degraded",
  );

  const verifier = spawnSync(
    process.execPath,
    [
      fileURLToPath(new URL("./verify-release-manifest.mjs", import.meta.url)),
      manifestFile,
      data.assetsDirectory,
      data.checksumFile,
      UNSIGNED_DEGRADED_TAG,
      COMMIT,
      REPOSITORY,
      "unsigned-degraded",
    ],
    { encoding: "utf8" },
  );
  assert.equal(verifier.status, 0, verifier.stderr);
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

test("unsigned-degraded is restricted to the stable v1.1.0 contract", () => {
  assert.deepEqual(
    expectedReleaseAssets("1.1.0"),
    expectedReleaseAssets("1.1.0", "unsigned-degraded"),
  );
  assert.throws(
    () => expectedReleaseAssets("1.1.0", "official"),
    /unsigned-degraded release contract/u,
  );
  assert.throws(
    () => expectedReleaseAssets("1.0.0", "unsigned-degraded"),
    /defined only for v1\.1\.0/u,
  );
  assert.throws(
    () => expectedReleaseAssets("1.1.0-rc.1", "unsigned-degraded"),
    /requires a stable tag/u,
  );
});
