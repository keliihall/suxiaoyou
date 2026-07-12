#!/usr/bin/env node

/** Independently verify a release-manifest.json and its seven installers. */

import { createHash } from "node:crypto";
import { lstatSync, readFileSync, readdirSync, statSync } from "node:fs";
import { basename, join, resolve } from "node:path";

import {
  expectedReleaseAssets,
  parseChecksumMarkdown,
  RELEASE_MANIFEST_KIND,
  RELEASE_MANIFEST_SCHEMA_VERSION,
  releaseIdentityFromVersion,
  releaseVersionFromTag,
} from "./generate-release-manifest.mjs";
import { isMainModule } from "./release-metadata.mjs";

export function verifyReleaseManifest({
  manifestFile,
  assetsDirectory,
  checksumFile,
  expectedTag,
  expectedCommit,
  expectedRepository,
}) {
  const manifest = JSON.parse(readFileSync(manifestFile, "utf8"));
  const version = releaseVersionFromTag(expectedTag);
  const { appVersion, channel } = releaseIdentityFromVersion(version);
  requireExactKeys(
    manifest,
    [
      "schemaVersion",
      "kind",
      "updateMode",
      "channel",
      "repository",
      "tag",
      "version",
      "appVersion",
      "commit",
      "releaseUrl",
      "checksumUrl",
      "assets",
    ],
    "release manifest",
  );
  if (manifest.schemaVersion !== RELEASE_MANIFEST_SCHEMA_VERSION) {
    throw new Error(`unsupported release manifest schema ${manifest.schemaVersion}`);
  }
  if (manifest.kind !== RELEASE_MANIFEST_KIND) throw new Error("release manifest kind is invalid");
  if (manifest.updateMode !== "manual-download") {
    throw new Error("release manifest must not claim automatic-update capability");
  }
  requireEqual(manifest.channel, channel, "channel");
  requireEqual(manifest.tag, expectedTag, "tag");
  requireEqual(manifest.version, version, "version");
  requireEqual(manifest.appVersion, appVersion, "app version");
  requireEqual(manifest.commit, expectedCommit, "commit");
  requireEqual(manifest.repository, expectedRepository, "repository");
  const releaseBase = `https://github.com/${expectedRepository}/releases`;
  requireEqual(manifest.releaseUrl, `${releaseBase}/tag/${expectedTag}`, "release URL");
  requireEqual(
    manifest.checksumUrl,
    `${releaseBase}/download/${expectedTag}/CHECKSUMS.md`,
    "checksum URL",
  );
  if (!Array.isArray(manifest.assets)) throw new Error("release manifest assets must be an array");

  const root = resolve(assetsDirectory);
  const expected = expectedReleaseAssets(version);
  const installerNames = readdirSync(root)
    .filter((name) => /\.(?:exe|dmg|deb|rpm)$/i.test(name))
    .sort();
  const expectedNames = expected.map(({ name }) => name).sort();
  if (JSON.stringify(installerNames) !== JSON.stringify(expectedNames)) {
    throw new Error(
      `release asset set mismatch; expected ${expectedNames.join(", ")}, got ${installerNames.join(", ")}`,
    );
  }
  const checksums = parseChecksumMarkdown(readFileSync(checksumFile, "utf8"));
  if (checksums.size !== expected.length) {
    throw new Error(`CHECKSUMS.md must contain exactly ${expected.length} installer rows`);
  }

  const seen = new Set();
  for (let index = 0; index < expected.length; index += 1) {
    const specification = expected[index];
    const asset = manifest.assets[index];
    if (!asset || typeof asset !== "object" || Array.isArray(asset)) {
      throw new Error(`release asset ${index} must be an object`);
    }
    requireExactKeys(
      asset,
      ["platform", "architecture", "format", "name", "size", "sha256", "downloadUrl"],
      `release asset ${index}`,
    );
    if (seen.has(asset.name)) throw new Error(`duplicate manifest asset ${asset.name}`);
    seen.add(asset.name);
    for (const field of ["platform", "architecture", "format", "name"]) {
      requireEqual(asset[field], specification[field], `asset ${index} ${field}`);
    }
    if (basename(asset.name) !== asset.name) throw new Error(`unsafe asset name ${asset.name}`);
    const path = join(root, asset.name);
    const lstat = lstatSync(path);
    if (!lstat.isFile() || lstat.isSymbolicLink()) {
      throw new Error(`release asset must be a regular non-symlink file: ${asset.name}`);
    }
    const stat = statSync(path);
    requireEqual(asset.size, stat.size, `asset ${asset.name} size`);
    const sha256 = createHash("sha256").update(readFileSync(path)).digest("hex");
    requireEqual(asset.sha256, sha256, `asset ${asset.name} SHA-256`);
    requireEqual(checksums.get(asset.name), sha256, `checksum row for ${asset.name}`);
    requireEqual(
      asset.downloadUrl,
      `${releaseBase}/download/${expectedTag}/${encodeURIComponent(asset.name)}`,
      `asset ${asset.name} download URL`,
    );
  }

  return manifest;
}

function requireEqual(actual, expected, label) {
  if (actual !== expected) {
    throw new Error(`${label} mismatch: expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)}`);
  }
}

function requireExactKeys(value, expectedKeys, label) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error(`${label} must be an object`);
  }
  const actual = Object.keys(value).sort();
  const expected = [...expectedKeys].sort();
  if (JSON.stringify(actual) !== JSON.stringify(expected)) {
    throw new Error(
      `${label} fields mismatch: expected ${expected.join(", ")}, got ${actual.join(", ")}`,
    );
  }
}

function main() {
  const [
    manifestFile,
    assetsDirectory,
    checksumFile,
    expectedTag,
    expectedCommit,
    expectedRepository,
  ] = process.argv.slice(2);
  if (
    !manifestFile ||
    !assetsDirectory ||
    !checksumFile ||
    !expectedTag ||
    !expectedCommit ||
    !expectedRepository
  ) {
    throw new Error(
      "usage: verify-release-manifest.mjs <manifest> <assets-dir> <checksums> <tag> <commit> <owner/repo>",
    );
  }
  const manifest = verifyReleaseManifest({
    manifestFile,
    assetsDirectory,
    checksumFile,
    expectedTag,
    expectedCommit,
    expectedRepository,
  });
  console.log(
    `[verify-release-manifest] ${manifest.tag} has ${manifest.assets.length} ` +
      "verified manual-download assets",
  );
}

if (isMainModule(import.meta.url)) {
  try {
    main();
  } catch (error) {
    console.error(
      `[verify-release-manifest] ${error instanceof Error ? error.message : String(error)}`,
    );
    process.exitCode = 1;
  }
}
