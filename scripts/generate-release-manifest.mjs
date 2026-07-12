#!/usr/bin/env node

/** Generate the manual-download release manifest attached to a tagged release. */

import { createHash } from "node:crypto";
import { readFileSync, statSync, writeFileSync } from "node:fs";
import { basename, join, resolve } from "node:path";

import { assertReleaseVersion, isMainModule } from "./release-metadata.mjs";

export const RELEASE_MANIFEST_SCHEMA_VERSION = 2;
export const RELEASE_MANIFEST_KIND = "suxiaoyou-release-manifest";

const RELEASE_ARTIFACT_VERSION_PATTERN =
  /^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-rc\.([1-9]\d*))?$/;

export function releaseIdentityFromVersion(version) {
  const match = RELEASE_ARTIFACT_VERSION_PATTERN.exec(version ?? "");
  if (!match) {
    throw new Error(
      `Invalid release version "${version ?? ""}". Expected format: X.Y.Z or X.Y.Z-rc.N`,
    );
  }
  const appVersion = `${match[1]}.${match[2]}.${match[3]}`;
  assertReleaseVersion(appVersion);
  return {
    version,
    appVersion,
    channel: match[4] ? "prerelease" : "stable",
  };
}

export function expectedReleaseAssets(version) {
  const { channel } = releaseIdentityFromVersion(version);
  const macosTrustSuffix = channel === "prerelease" ? "-ADHOC-NOT-NOTARIZED" : "";
  return [
    {
      platform: "windows",
      architecture: "x86_64",
      format: "nsis",
      name: `suxiaoyou-${version}-windows-x64-setup.exe`,
    },
    {
      platform: "macos",
      architecture: "arm64",
      format: "dmg",
      name: `suxiaoyou-${version}-macos-aarch64${macosTrustSuffix}.dmg`,
    },
    {
      platform: "macos",
      architecture: "x86_64",
      format: "dmg",
      name: `suxiaoyou-${version}-macos-x64${macosTrustSuffix}.dmg`,
    },
    {
      platform: "linux",
      architecture: "x86_64",
      format: "deb",
      name: `suxiaoyou-${version}-linux-amd64.deb`,
    },
    {
      platform: "linux",
      architecture: "x86_64",
      format: "rpm",
      name: `suxiaoyou-${version}-linux-x86_64.rpm`,
    },
    {
      platform: "linux",
      architecture: "arm64",
      format: "deb",
      name: `suxiaoyou-${version}-linux-arm64.deb`,
    },
    {
      platform: "linux",
      architecture: "arm64",
      format: "rpm",
      name: `suxiaoyou-${version}-linux-aarch64.rpm`,
    },
  ];
}

export function parseChecksumMarkdown(markdown) {
  const checksums = new Map();
  for (const line of markdown.split(/\r?\n/)) {
    const match = /^\| `([^`]+)` \| `([0-9a-f]{64})` \|/.exec(line);
    if (!match) continue;
    if (checksums.has(match[1])) throw new Error(`duplicate checksum row for ${match[1]}`);
    checksums.set(match[1], match[2]);
  }
  return checksums;
}

export function generateReleaseManifest({
  assetsDirectory,
  tag,
  commit,
  repository,
  checksumFile,
}) {
  const version = releaseVersionFromTag(tag);
  const { appVersion, channel } = releaseIdentityFromVersion(version);
  assertCommit(commit);
  assertRepository(repository);
  const assetsRoot = resolve(assetsDirectory);
  const checksums = parseChecksumMarkdown(readFileSync(checksumFile, "utf8"));
  const releaseBase = `https://github.com/${repository}/releases`;
  const assets = expectedReleaseAssets(version).map((specification) => {
    const path = join(assetsRoot, specification.name);
    const stat = statSync(path, { throwIfNoEntry: false });
    if (!stat?.isFile()) throw new Error(`release asset is missing: ${specification.name}`);
    const sha256 = sha256File(path);
    if (checksums.get(specification.name) !== sha256) {
      throw new Error(`CHECKSUMS.md does not match ${specification.name}`);
    }
    return {
      ...specification,
      size: stat.size,
      sha256,
      downloadUrl: `${releaseBase}/download/${tag}/${encodeURIComponent(specification.name)}`,
    };
  });
  if (checksums.size !== assets.length) {
    throw new Error(`CHECKSUMS.md must contain exactly ${assets.length} installer rows`);
  }

  return {
    schemaVersion: RELEASE_MANIFEST_SCHEMA_VERSION,
    kind: RELEASE_MANIFEST_KIND,
    updateMode: "manual-download",
    channel,
    repository,
    tag,
    version,
    appVersion,
    commit,
    releaseUrl: `${releaseBase}/tag/${tag}`,
    checksumUrl: `${releaseBase}/download/${tag}/CHECKSUMS.md`,
    assets,
  };
}

export function releaseVersionFromTag(tag) {
  if (typeof tag !== "string" || !tag.startsWith("v")) {
    throw new Error(
      `invalid release tag ${JSON.stringify(tag)}; expected vX.Y.Z or vX.Y.Z-rc.N`,
    );
  }
  const version = tag.slice(1);
  releaseIdentityFromVersion(version);
  return version;
}

function sha256File(path) {
  return createHash("sha256").update(readFileSync(path)).digest("hex");
}

function assertCommit(commit) {
  if (typeof commit !== "string" || !/^[0-9a-f]{40}$/.test(commit)) {
    throw new Error("release commit must be a lowercase 40-character Git SHA");
  }
}

function assertRepository(repository) {
  if (
    typeof repository !== "string" ||
    !/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(repository)
  ) {
    throw new Error("repository must use owner/name form");
  }
}

function main() {
  const [assetsDirectory, tag, commit, repository, checksumFile, outputFile] =
    process.argv.slice(2);
  if (!assetsDirectory || !tag || !commit || !repository || !checksumFile || !outputFile) {
    throw new Error(
      "usage: generate-release-manifest.mjs <assets-dir> <tag> <commit> <owner/repo> <checksums> <output>",
    );
  }
  const manifest = generateReleaseManifest({
    assetsDirectory,
    tag,
    commit,
    repository,
    checksumFile,
  });
  writeFileSync(outputFile, `${JSON.stringify(manifest, null, 2)}\n`);
  console.log(
    `[generate-release-manifest] wrote ${basename(outputFile)} for ${manifest.tag} ` +
      `with ${manifest.assets.length} manual-download assets`,
  );
}

if (isMainModule(import.meta.url)) {
  try {
    main();
  } catch (error) {
    console.error(
      `[generate-release-manifest] ${error instanceof Error ? error.message : String(error)}`,
    );
    process.exitCode = 1;
  }
}
