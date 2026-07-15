#!/usr/bin/env node

import { createHash } from "node:crypto";
import {
  createReadStream,
  mkdirSync,
  readFileSync,
  readdirSync,
  renameSync,
  statSync,
  writeFileSync,
} from "node:fs";
import { basename, dirname, join, relative, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";

import { validateDesktopLifecycleReport } from "./verify-desktop-lifecycle.mjs";

export const NATIVE_PACKAGE_EVIDENCE_SCHEMA_VERSION = 1;

const COMMIT_PATTERN = /^(?!0{40}$)[0-9a-f]{40}$/;
const SHA256_PATTERN = /^(?!0{64}$)[0-9a-f]{64}$/;
const RELEASE_CHANNELS = new Set(["prerelease", "stable"]);

const PACKAGE_DEFINITIONS = Object.freeze([
  {
    kind: "windows-x64-nsis",
    platform: "win32",
    artifactName: (version) => `suyo-${version}-windows-x64-setup.exe`,
    lifecycleArtifact: /^windows-lifecycle-diagnostics-[1-9][0-9]*$/,
    lifecycleDirectory: "suxiaoyou-desktop-lifecycle-windows",
  },
  {
    kind: "macos-arm64-dmg",
    platform: "darwin",
    artifactName: (version, channel) =>
      `suyo-${version}-macos-aarch64${channel === "prerelease" ? "-ADHOC-NOT-NOTARIZED" : ""}.dmg`,
    lifecycleArtifact: /^macos-aarch64-lifecycle-diagnostics-[1-9][0-9]*$/,
    lifecycleDirectory: "suxiaoyou-desktop-lifecycle-macos-aarch64",
  },
  {
    kind: "macos-x64-dmg",
    platform: "darwin",
    artifactName: (version, channel) =>
      `suyo-${version}-macos-x64${channel === "prerelease" ? "-ADHOC-NOT-NOTARIZED" : ""}.dmg`,
    lifecycleArtifact: /^macos-x64-lifecycle-diagnostics-[1-9][0-9]*$/,
    lifecycleDirectory: "suxiaoyou-desktop-lifecycle-macos-x64",
  },
  {
    kind: "linux-x64-deb",
    platform: "linux",
    bundleType: "deb",
    artifactName: (version) => `suyo-${version}-linux-amd64.deb`,
    lifecycleArtifact: /^linux-x64-lifecycle-diagnostics-[1-9][0-9]*$/,
    lifecycleDirectory: "suxiaoyou-desktop-lifecycle-linux-deb",
  },
  {
    kind: "linux-x64-rpm",
    platform: "linux",
    bundleType: "rpm",
    artifactName: (version) => `suyo-${version}-linux-x86_64.rpm`,
    lifecycleArtifact: /^linux-x64-lifecycle-diagnostics-[1-9][0-9]*$/,
    lifecycleDirectory: "suxiaoyou-desktop-lifecycle-linux-rpm",
  },
  {
    kind: "linux-arm64-deb",
    platform: "linux",
    bundleType: "deb",
    artifactName: (version) => `suyo-${version}-linux-arm64.deb`,
    lifecycleArtifact: /^linux-arm64-lifecycle-diagnostics-[1-9][0-9]*$/,
    lifecycleDirectory: "suxiaoyou-desktop-lifecycle-linux-deb",
  },
  {
    kind: "linux-arm64-rpm",
    platform: "linux",
    bundleType: "rpm",
    artifactName: (version) => `suyo-${version}-linux-aarch64.rpm`,
    lifecycleArtifact: /^linux-arm64-lifecycle-diagnostics-[1-9][0-9]*$/,
    lifecycleDirectory: "suxiaoyou-desktop-lifecycle-linux-rpm",
  },
]);

function allRegularFiles(root) {
  const files = [];
  for (const entry of readdirSync(root, { withFileTypes: true })) {
    const path = join(root, entry.name);
    if (entry.isSymbolicLink()) {
      throw new Error(`symbolic links are not accepted in release evidence: ${path}`);
    }
    if (entry.isDirectory()) files.push(...allRegularFiles(path));
    else if (entry.isFile()) files.push(path);
    else throw new Error(`unsupported filesystem object in release evidence: ${path}`);
  }
  return files;
}

async function sha256File(path) {
  const hash = createHash("sha256");
  for await (const chunk of createReadStream(path)) hash.update(chunk);
  return hash.digest("hex");
}

function sha256Text(text) {
  return createHash("sha256").update(text).digest("hex");
}

function parseChecksums(markdown) {
  const entries = new Map();
  for (const line of markdown.split(/\r?\n/u)) {
    const match = /^\|\s*`([^`]+)`\s*\|\s*`([0-9a-f]{64})`\s*\|/u.exec(line);
    if (!match) continue;
    if (entries.has(match[1])) throw new Error(`duplicate checksum row for ${match[1]}`);
    entries.set(match[1], match[2]);
  }
  return entries;
}

function relativeSegments(root, path) {
  const value = relative(root, path);
  if (!value || value.startsWith(`..${sep}`) || value === "..") {
    throw new Error(`evidence path escaped its root: ${path}`);
  }
  return value.split(sep);
}

function matchesLifecyclePath(root, path, definition) {
  const parts = relativeSegments(root, path);
  return (
    parts.length === 3 &&
    definition.lifecycleArtifact.test(parts[0]) &&
    parts[1] === definition.lifecycleDirectory &&
    parts[2] === "result.json"
  );
}

function releaseVersion(releaseTag, releaseChannel) {
  const tag = String(releaseTag ?? "");
  const valid =
    releaseChannel === "stable"
      ? /^v[0-9]+\.[0-9]+\.[0-9]+$/u.test(tag)
      : /^v[0-9]+\.[0-9]+\.[0-9]+-rc\.[1-9][0-9]*$/u.test(tag);
  if (!valid) {
    throw new Error(`release tag ${tag || "missing"} does not match ${releaseChannel}`);
  }
  return tag.slice(1);
}

export async function collectNativePackageEvidence({
  assetsRoot,
  artifactsRoot,
  checksumFile,
  releaseCommit,
  releaseTag,
  releaseChannel,
}) {
  const commit = String(releaseCommit ?? "").trim().toLowerCase();
  if (!COMMIT_PATTERN.test(commit)) {
    throw new Error("releaseCommit must be a full non-zero Git commit ID");
  }
  if (!RELEASE_CHANNELS.has(releaseChannel)) {
    throw new Error("releaseChannel must be prerelease or stable");
  }
  const version = releaseVersion(releaseTag, releaseChannel);
  const assetsDirectory = resolve(assetsRoot);
  const artifactsDirectory = resolve(artifactsRoot);
  if (!statSync(assetsDirectory).isDirectory()) {
    throw new Error(`${assetsDirectory} is not a directory`);
  }
  if (!statSync(artifactsDirectory).isDirectory()) {
    throw new Error(`${artifactsDirectory} is not a directory`);
  }
  const assetFiles = allRegularFiles(assetsDirectory);
  const artifactFiles = allRegularFiles(artifactsDirectory);
  const checksumEntries = parseChecksums(readFileSync(checksumFile, "utf8"));
  const expectedAssetNames = new Set(
    PACKAGE_DEFINITIONS.map((definition) =>
      definition.artifactName(version, releaseChannel),
    ),
  );
  const installerFiles = assetFiles.filter((path) => /\.(?:exe|dmg|deb|rpm)$/iu.test(path));
  const unexpectedAssets = installerFiles
    .map((path) => basename(path))
    .filter((name) => !expectedAssetNames.has(name));
  if (
    installerFiles.length !== PACKAGE_DEFINITIONS.length ||
    unexpectedAssets.length > 0
  ) {
    throw new Error(
      `expected exactly ${PACKAGE_DEFINITIONS.length} native installers; ` +
        `found ${installerFiles.length}, unexpected=${unexpectedAssets.join(",") || "none"}`,
    );
  }
  const unexpectedChecksums = [...checksumEntries.keys()].filter(
    (name) => !expectedAssetNames.has(name),
  );
  if (
    checksumEntries.size !== PACKAGE_DEFINITIONS.length ||
    unexpectedChecksums.length > 0
  ) {
    throw new Error(
      `checksum table must contain exactly the seven native installers; ` +
        `found ${checksumEntries.size}, unexpected=${unexpectedChecksums.join(",") || "none"}`,
    );
  }

  const packages = [];
  const consumedReports = new Set();
  for (const definition of PACKAGE_DEFINITIONS) {
    const artifactName = definition.artifactName(version, releaseChannel);
    const matchingAssets = installerFiles.filter((path) => basename(path) === artifactName);
    if (matchingAssets.length !== 1) {
      throw new Error(`${definition.kind}: expected one ${artifactName}, found ${matchingAssets.length}`);
    }
    const artifactPath = matchingAssets[0];
    const artifactSize = statSync(artifactPath).size;
    if (artifactSize <= 0) throw new Error(`${definition.kind}: installer is empty`);
    const artifactSha256 = await sha256File(artifactPath);
    if (!SHA256_PATTERN.test(artifactSha256)) {
      throw new Error(`${definition.kind}: installer SHA-256 is invalid`);
    }
    if (checksumEntries.get(artifactName) !== artifactSha256) {
      throw new Error(`${definition.kind}: CHECKSUMS.md does not match the installer`);
    }

    const matchingReports = artifactFiles.filter((path) =>
      matchesLifecyclePath(artifactsDirectory, path, definition),
    );
    if (matchingReports.length !== 1) {
      throw new Error(
        `${definition.kind}: expected one lifecycle result, found ${matchingReports.length}`,
      );
    }
    const lifecyclePath = matchingReports[0];
    if (consumedReports.has(lifecyclePath)) {
      throw new Error(`${definition.kind}: lifecycle result was reused by another package`);
    }
    consumedReports.add(lifecyclePath);
    const lifecycleRaw = readFileSync(lifecyclePath, "utf8");
    let lifecycleValue;
    try {
      lifecycleValue = JSON.parse(lifecycleRaw);
    } catch (error) {
      throw new Error(
        `${definition.kind}: lifecycle result is not JSON: ${error instanceof Error ? error.message : error}`,
      );
    }
    const lifecycle = validateDesktopLifecycleReport(lifecycleValue, {
      expectedPlatform: definition.platform,
      expectedCommit: commit,
      expectedReleaseRef: releaseTag,
      expectedBundleType: definition.bundleType,
    });
    if (!lifecycle.ok) {
      throw new Error(
        `${definition.kind}: lifecycle result is invalid: ${lifecycle.failures.join("; ")}`,
      );
    }

    packages.push({
      kind: definition.kind,
      tag: releaseTag,
      source_commit: commit,
      artifact_name: artifactName,
      artifact_sha256: artifactSha256,
      artifact_size: artifactSize,
      lifecycle_report_sha256: sha256Text(lifecycleRaw),
      executable_path: lifecycle.report.executable_path,
      executable_size: lifecycle.report.executable_size,
      executable_sha256: lifecycle.report.executable_sha256,
      ...(definition.bundleType
        ? {
            tauri_bundle_type: lifecycle.report.tauri_bundle_type,
            executable_unpatched_sha256:
              lifecycle.report.executable_unpatched_sha256,
          }
        : {}),
      checksum_verified: true,
      installed: true,
      launched: lifecycle.report.checks.backend_ready,
      exited_cleanly: lifecycle.report.checks.graceful_exit,
      no_orphan_processes: lifecycle.report.checks.no_orphan_processes,
      ...(definition.kind.startsWith("macos-")
        ? releaseChannel === "stable"
          ? {
              artifact_profile: "release",
              developer_id_signed: true,
              notarized: true,
            }
          : {
              artifact_profile: "rc-adhoc",
              trust_boundary_verified: true,
            }
        : {}),
    });
  }

  const lifecycleResults = artifactFiles.filter((path) => {
    const parts = relativeSegments(artifactsDirectory, path);
    return (
      parts.length === 3 &&
      /-lifecycle-diagnostics-[1-9][0-9]*$/u.test(parts[0]) &&
      parts[1].startsWith("suxiaoyou-desktop-lifecycle-") &&
      parts[2] === "result.json"
    );
  });
  if (
    lifecycleResults.length !== PACKAGE_DEFINITIONS.length ||
    lifecycleResults.some((path) => !consumedReports.has(path))
  ) {
    throw new Error(
      `expected exactly seven consumed native lifecycle results, found ${lifecycleResults.length}`,
    );
  }
  const packagesByKind = new Map(packages.map((item) => [item.kind, item]));
  for (const [debKind, rpmKind] of [
    ["linux-x64-deb", "linux-x64-rpm"],
    ["linux-arm64-deb", "linux-arm64-rpm"],
  ]) {
    const deb = packagesByKind.get(debKind);
    const rpm = packagesByKind.get(rpmKind);
    if (
      deb.executable_unpatched_sha256 !== rpm.executable_unpatched_sha256 ||
      deb.executable_size !== rpm.executable_size
    ) {
      throw new Error(
        `${debKind}/${rpmKind}: installed executable identity differs after restoring the Tauri bundle marker`,
      );
    }
  }

  return {
    schema_version: NATIVE_PACKAGE_EVIDENCE_SCHEMA_VERSION,
    release_tag: releaseTag,
    release_commit: commit,
    release_channel: releaseChannel,
    generated_at: new Date().toISOString(),
    packages,
  };
}

function writeJsonAtomic(path, value) {
  const destination = resolve(path);
  mkdirSync(dirname(destination), { recursive: true });
  const temporary = `${destination}.tmp-${process.pid}`;
  writeFileSync(temporary, `${JSON.stringify(value, null, 2)}\n`, {
    encoding: "utf8",
    mode: 0o600,
  });
  renameSync(temporary, destination);
}

async function runCli() {
  const [
    command,
    assetsRoot,
    artifactsRoot,
    checksumFile,
    releaseCommit,
    releaseTag,
    releaseChannel,
    outputPath,
  ] = process.argv.slice(2);
  if (
    command !== "aggregate" ||
    !assetsRoot ||
    !artifactsRoot ||
    !checksumFile ||
    !releaseCommit ||
    !releaseTag ||
    !releaseChannel ||
    !outputPath
  ) {
    console.error(
      "Usage: node scripts/native-package-evidence.mjs aggregate " +
        "<release-assets> <downloaded-artifacts> <CHECKSUMS.md> " +
        "<40-char-commit> <release-tag> <prerelease|stable> <output.json>",
    );
    process.exitCode = 2;
    return;
  }
  try {
    const evidence = await collectNativePackageEvidence({
      assetsRoot,
      artifactsRoot,
      checksumFile,
      releaseCommit,
      releaseTag,
      releaseChannel,
    });
    writeJsonAtomic(outputPath, evidence);
    console.log(
      `Verified ${evidence.packages.length} native package lifecycle records and wrote ${resolve(outputPath)}`,
    );
  } catch (error) {
    console.error(error instanceof Error ? error.message : String(error));
    process.exitCode = 1;
  }
}

const isCli = process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url);
if (isCli) await runCli();
