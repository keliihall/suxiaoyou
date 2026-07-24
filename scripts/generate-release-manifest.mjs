#!/usr/bin/env node

/** Generate the manual-download release manifest attached to a tagged release. */

import { createHash } from "node:crypto";
import { readFileSync, statSync, writeFileSync } from "node:fs";
import { basename, join, resolve } from "node:path";

import { assertReleaseVersion, isMainModule } from "./release-metadata.mjs";

export const RELEASE_MANIFEST_SCHEMA_VERSION = 2;
export const UNSIGNED_DEGRADED_RELEASE_MANIFEST_SCHEMA_VERSION = 3;
export const RELEASE_MANIFEST_KIND = "suxiaoyou-release-manifest";
export const RELEASE_PROFILES = Object.freeze({
  OFFICIAL: "official",
  RC_ADHOC: "rc-adhoc",
  UNSIGNED_DEGRADED: "unsigned-degraded",
});

export const UNSIGNED_DEGRADED_TRUST = Object.freeze({
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

export const UNSIGNED_DEGRADED_CAPABILITIES = Object.freeze({
  v11Gates: "released",
  officeRenderer: "absent",
  officeAuthoring: "unavailable",
  integrations: "not-run",
});

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

export function resolveReleaseProfile(version, requestedProfile) {
  const { appVersion, channel } = releaseIdentityFromVersion(version);
  const isV11ReleaseLine = /^1\.1\.(?:0|[1-9]\d*)$/u.test(appVersion);
  const defaultProfile =
    isV11ReleaseLine
      ? RELEASE_PROFILES.UNSIGNED_DEGRADED
      : channel === "stable"
        ? RELEASE_PROFILES.OFFICIAL
        : RELEASE_PROFILES.RC_ADHOC;
  const profile = String(requestedProfile ?? "").trim() || defaultProfile;
  const expectedChannel = profile === RELEASE_PROFILES.RC_ADHOC
    ? "prerelease"
    : profile === RELEASE_PROFILES.OFFICIAL
      ? "stable"
      : channel;
  if (!Object.values(RELEASE_PROFILES).includes(profile)) {
    throw new Error(
      `unsupported release profile ${JSON.stringify(profile)}; expected official, rc-adhoc, or unsigned-degraded`,
    );
  }
  if (channel !== expectedChannel) {
    throw new Error(
      `release profile ${profile} requires a ${expectedChannel} tag, got ${channel}`,
    );
  }
  if (
    profile === RELEASE_PROFILES.UNSIGNED_DEGRADED &&
    !isV11ReleaseLine
  ) {
    throw new Error(
      `release profile ${profile} is defined only for v1.1.x release line tags, got v${version}`,
    );
  }
  if (isV11ReleaseLine && profile !== RELEASE_PROFILES.UNSIGNED_DEGRADED) {
    throw new Error(
      "v1.1.x release line tags are defined by the unsigned-degraded release contract",
    );
  }
  return profile;
}

function profiledInstallerName(stem, extension, profile) {
  const suffix =
    profile === RELEASE_PROFILES.UNSIGNED_DEGRADED
      ? "-UNSIGNED-DEGRADED"
      : "";
  return `${stem}${suffix}.${extension}`;
}

export function expectedReleaseAssets(version, requestedProfile) {
  const profile = resolveReleaseProfile(version, requestedProfile);
  const macosTrustSuffix =
    profile === RELEASE_PROFILES.RC_ADHOC ? "-ADHOC-NOT-NOTARIZED" : "";
  return [
    {
      platform: "windows",
      architecture: "x86_64",
      format: "nsis",
      name: profiledInstallerName(
        `suyo-${version}-windows-x64-setup`,
        "exe",
        profile,
      ),
    },
    {
      platform: "windows",
      architecture: "arm64",
      format: "nsis",
      name: profiledInstallerName(
        `suyo-${version}-windows-arm64-setup`,
        "exe",
        profile,
      ),
    },
    {
      platform: "macos",
      architecture: "arm64",
      format: "dmg",
      name: profiledInstallerName(
        `suyo-${version}-macos-aarch64${macosTrustSuffix}`,
        "dmg",
        profile,
      ),
    },
    {
      platform: "macos",
      architecture: "x86_64",
      format: "dmg",
      name: profiledInstallerName(
        `suyo-${version}-macos-x64${macosTrustSuffix}`,
        "dmg",
        profile,
      ),
    },
    {
      platform: "linux",
      architecture: "x86_64",
      format: "deb",
      name: profiledInstallerName(
        `suyo-${version}-linux-amd64`,
        "deb",
        profile,
      ),
    },
    {
      platform: "linux",
      architecture: "x86_64",
      format: "rpm",
      name: profiledInstallerName(
        `suyo-${version}-linux-x86_64`,
        "rpm",
        profile,
      ),
    },
    {
      platform: "linux",
      architecture: "arm64",
      format: "deb",
      name: profiledInstallerName(
        `suyo-${version}-linux-arm64`,
        "deb",
        profile,
      ),
    },
    {
      platform: "linux",
      architecture: "arm64",
      format: "rpm",
      name: profiledInstallerName(
        `suyo-${version}-linux-aarch64`,
        "rpm",
        profile,
      ),
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
  releaseProfile,
}) {
  const version = releaseVersionFromTag(tag);
  const { appVersion, channel } = releaseIdentityFromVersion(version);
  const profile = resolveReleaseProfile(version, releaseProfile);
  assertCommit(commit);
  assertRepository(repository);
  const assetsRoot = resolve(assetsDirectory);
  const checksums = parseChecksumMarkdown(readFileSync(checksumFile, "utf8"));
  const releaseBase = `https://github.com/${repository}/releases`;
  const assets = expectedReleaseAssets(version, profile).map((specification) => {
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

  const common = {
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
  if (profile !== RELEASE_PROFILES.UNSIGNED_DEGRADED) {
    return {
      schemaVersion: RELEASE_MANIFEST_SCHEMA_VERSION,
      ...common,
    };
  }
  return {
    schemaVersion: UNSIGNED_DEGRADED_RELEASE_MANIFEST_SCHEMA_VERSION,
    ...common,
    releaseProfile: profile,
    publicationChannel: "prerelease",
    officialReleaseEligible: false,
    latestEligible: false,
    trust: { ...UNSIGNED_DEGRADED_TRUST },
    capabilities: { ...UNSIGNED_DEGRADED_CAPABILITIES },
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
  const [
    assetsDirectory,
    tag,
    commit,
    repository,
    checksumFile,
    outputFile,
    releaseProfile,
  ] = process.argv.slice(2);
  if (!assetsDirectory || !tag || !commit || !repository || !checksumFile || !outputFile) {
    throw new Error(
      "usage: generate-release-manifest.mjs <assets-dir> <tag> <commit> <owner/repo> <checksums> <output> [official|rc-adhoc|unsigned-degraded]",
    );
  }
  const manifest = generateReleaseManifest({
    assetsDirectory,
    tag,
    commit,
    repository,
    checksumFile,
    releaseProfile,
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
