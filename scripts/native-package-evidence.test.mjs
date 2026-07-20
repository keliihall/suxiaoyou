import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import {
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";

import { collectNativePackageEvidence } from "./native-package-evidence.mjs";

const COMMIT = "a".repeat(40);
const TAG = "v1.0.0-rc.7";
const VERSION = TAG.slice(1);
const UNSIGNED_DEGRADED_TAG = "v1.1.0";
const UNSIGNED_DEGRADED_RC_TAG = "v1.1.0-rc.2";

const FIXTURES = [
  ["windows-x64-nsis", `suyo-${VERSION}-windows-x64-setup.exe`, "windows-lifecycle-diagnostics-1", "suxiaoyou-desktop-lifecycle-windows", "win32"],
  ["macos-arm64-dmg", `suyo-${VERSION}-macos-aarch64-ADHOC-NOT-NOTARIZED.dmg`, "macos-aarch64-lifecycle-diagnostics-1", "suxiaoyou-desktop-lifecycle-macos-aarch64", "darwin"],
  ["macos-x64-dmg", `suyo-${VERSION}-macos-x64-ADHOC-NOT-NOTARIZED.dmg`, "macos-x64-lifecycle-diagnostics-1", "suxiaoyou-desktop-lifecycle-macos-x64", "darwin"],
  ["linux-x64-deb", `suyo-${VERSION}-linux-amd64.deb`, "linux-x64-lifecycle-diagnostics-1", "suxiaoyou-desktop-lifecycle-linux-deb", "linux", "deb"],
  ["linux-x64-rpm", `suyo-${VERSION}-linux-x86_64.rpm`, "linux-x64-lifecycle-diagnostics-1", "suxiaoyou-desktop-lifecycle-linux-rpm", "linux", "rpm"],
  ["linux-arm64-deb", `suyo-${VERSION}-linux-arm64.deb`, "linux-arm64-lifecycle-diagnostics-1", "suxiaoyou-desktop-lifecycle-linux-deb", "linux", "deb"],
  ["linux-arm64-rpm", `suyo-${VERSION}-linux-aarch64.rpm`, "linux-arm64-lifecycle-diagnostics-1", "suxiaoyou-desktop-lifecycle-linux-rpm", "linux", "rpm"],
];

const UNSIGNED_DEGRADED_FIXTURES = FIXTURES.map((entry) => {
  const copy = [...entry];
  copy[1] = copy[1]
    .replace(VERSION, UNSIGNED_DEGRADED_TAG.slice(1))
    .replace("-ADHOC-NOT-NOTARIZED", "")
    .replace(/\.(exe|dmg|deb|rpm)$/u, "-UNSIGNED-DEGRADED.$1");
  return copy;
});

const UNSIGNED_DEGRADED_RC_FIXTURES = UNSIGNED_DEGRADED_FIXTURES.map((entry) => {
  const copy = [...entry];
  copy[1] = copy[1].replace(
    UNSIGNED_DEGRADED_TAG.slice(1),
    UNSIGNED_DEGRADED_RC_TAG.slice(1),
  );
  return copy;
});

function sha256(value) {
  return createHash("sha256").update(value).digest("hex");
}

function lifecycle(
  platform,
  seed,
  releaseTag = TAG,
  executableSeed = seed,
  bundleType,
  artifactName = `installer-${seed}.pkg`,
  artifactBytes = `installer:${seed}`,
) {
  return {
    schema_version: 3,
    status: "ok",
    ok: true,
    platform,
    source_commit: COMMIT,
    release_ref: releaseTag,
    executable_path:
      platform === "win32"
        ? `D:\\a\\_temp\\suxiaoyou-nsis-install\\suxiaoyou-desktop-${executableSeed}.exe`
        : `/opt/suyo/desktop-${executableSeed}`,
    executable_size: 4096 + executableSeed,
    executable_sha256: seed.toString(16).padStart(64, "0"),
    artifact_path:
      platform === "win32"
        ? `D:\\a\\_temp\\native-packages\\${artifactName}`
        : `/tmp/native-packages/${artifactName}`,
    artifact_size: Buffer.byteLength(artifactBytes),
    artifact_sha256: sha256(artifactBytes),
    artifact_preinstall_seal: {
      source_commit: COMMIT,
      artifact_size: Buffer.byteLength(artifactBytes),
      artifact_sha256: sha256(artifactBytes),
    },
    ...(bundleType
      ? {
          tauri_bundle_type: bundleType,
          executable_unpatched_sha256: executableSeed
            .toString(16)
            .padStart(64, "0"),
        }
      : {}),
    started_at: "2026-07-14T00:00:00Z",
    completed_at: "2026-07-14T00:00:01Z",
    desktopPid: 4100 + seed * 2,
    backendPid: 4101 + seed * 2,
    backendUrl: `http://127.0.0.1:${43000 + seed}`,
    observedDescendantPids: [4101 + seed * 2],
    exit: { code: 0, signal: null },
    checks: {
      backend_ready: true,
      backend_healthy: true,
      graceful_exit: true,
      no_orphan_processes: true,
      backend_stopped: true,
      artifact_unchanged: true,
      artifact_matches_preinstall_seal: true,
    },
  };
}

function fixture(t, { definitions = FIXTURES, releaseTag = TAG } = {}) {
  const root = mkdtempSync(join(tmpdir(), "suyo-native-package-evidence-"));
  t.after(() => rmSync(root, { recursive: true, force: true }));
  const assetsRoot = join(root, "release-assets");
  const artifactsRoot = join(root, "artifacts");
  mkdirSync(assetsRoot);
  mkdirSync(artifactsRoot);
  const checksumRows = [];
  for (const [kind, name, artifactDirectory, lifecycleDirectory, platform, bundleType] of definitions) {
    const bytes = `installer:${kind}`;
    writeFileSync(join(assetsRoot, name), bytes);
    checksumRows.push(`| \`${name}\` | \`${sha256(bytes)}\` | 0.0 MiB |`);
    const executableSeed = kind.startsWith("linux-x64-")
      ? 40
      : kind.startsWith("linux-arm64-")
        ? 60
        : checksumRows.length;
    const reportDirectory = join(artifactsRoot, artifactDirectory, lifecycleDirectory);
    mkdirSync(reportDirectory, { recursive: true });
    writeFileSync(
      join(reportDirectory, "result.json"),
      `${JSON.stringify(
        lifecycle(
          platform,
          checksumRows.length,
          releaseTag,
          executableSeed,
          bundleType,
          name,
          bytes,
        ),
        null,
        2,
      )}\n`,
    );
  }
  const checksumFile = join(root, "CHECKSUMS.md");
  writeFileSync(
    checksumFile,
    ["## SHA-256 Checksums", "", "| File | SHA-256 | Size |", "|---|---|---|", ...checksumRows].join("\n"),
  );
  return { root, assetsRoot, artifactsRoot, checksumFile };
}

async function collect(paths, overrides = {}) {
  return collectNativePackageEvidence({
    ...paths,
    releaseCommit: COMMIT,
    releaseTag: TAG,
    releaseChannel: "prerelease",
    ...overrides,
  });
}

test("aggregates seven checksum-bound installed lifecycle records", async (t) => {
  const result = await collect(fixture(t));
  assert.equal(result.schema_version, 2);
  assert.equal(result.release_commit, COMMIT);
  assert.equal(result.release_tag, TAG);
  assert.equal("release_profile" in result, false);
  assert.equal("official_release_eligible" in result, false);
  assert.deepEqual(result.packages.map((item) => item.kind), FIXTURES.map(([kind]) => kind));
  assert.ok(result.packages.every((item) => item.checksum_verified));
  assert.ok(result.packages.every((item) => item.no_orphan_processes));
  assert.ok(result.packages.every((item) => item.lifecycle_artifact_bound));
  assert.match(result.packages[0].artifact_sha256, /^[0-9a-f]{64}$/u);
  assert.match(result.packages[0].lifecycle_report_sha256, /^[0-9a-f]{64}$/u);
  assert.match(result.packages[0].executable_sha256, /^[0-9a-f]{64}$/u);
  assert.ok(result.packages[0].executable_size > 0);
  assert.equal(result.packages[1].artifact_profile, "rc-adhoc");
  assert.equal(result.packages[1].trust_boundary_verified, true);
  const linuxX64 = result.packages.filter((item) => item.kind.startsWith("linux-x64-"));
  assert.notEqual(linuxX64[0].executable_sha256, linuxX64[1].executable_sha256);
  assert.equal(
    linuxX64[0].executable_unpatched_sha256,
    linuxX64[1].executable_unpatched_sha256,
  );
});

test("aggregates canonical Windows lifecycle evidence with win32 semantics", async (t) => {
  const result = await collect(fixture(t));
  const windows = result.packages.find((item) => item.kind === "windows-x64-nsis");
  assert.equal(
    windows.executable_path,
    "D:\\a\\_temp\\suxiaoyou-nsis-install\\suxiaoyou-desktop-1.exe",
  );
});

test("rejects ambiguous Windows lifecycle paths during aggregation", async (t) => {
  const paths = fixture(t);
  const reportPath = join(paths.artifactsRoot, FIXTURES[0][2], FIXTURES[0][3], "result.json");
  const report = JSON.parse(readFileSync(reportPath, "utf8"));
  report.executable_path = "D:/a\\_temp\\suxiaoyou-desktop.exe";
  writeFileSync(reportPath, `${JSON.stringify(report, null, 2)}\n`);
  await assert.rejects(() => collect(paths), /absolute canonical path/u);
});

test("CLI writes the same scorecard-ready evidence contract", (t) => {
  const paths = fixture(t);
  const output = join(paths.root, "PACKAGE-LIFECYCLE.json");
  const result = spawnSync(
    process.execPath,
    [
      fileURLToPath(new URL("./native-package-evidence.mjs", import.meta.url)),
      "aggregate",
      paths.assetsRoot,
      paths.artifactsRoot,
      paths.checksumFile,
      COMMIT,
      TAG,
      "prerelease",
      output,
    ],
    { encoding: "utf8" },
  );
  assert.equal(result.status, 0, result.stderr);
  const evidence = JSON.parse(readFileSync(output, "utf8"));
  assert.equal(evidence.packages.length, 7);
  assert.equal(evidence.release_commit, COMMIT);
  assert.equal(evidence.release_tag, TAG);
});

test("stable package evidence records the completed macOS trust gates", async (t) => {
  const stableTag = "v1.0.0";
  const stableVersion = stableTag.slice(1);
  const definitions = FIXTURES.map((entry) => {
    const copy = [...entry];
    copy[1] = copy[1]
      .replace(VERSION, stableVersion)
      .replace("-ADHOC-NOT-NOTARIZED", "");
    return copy;
  });
  const paths = fixture(t, { definitions, releaseTag: stableTag });
  const result = await collect(paths, {
    releaseTag: stableTag,
    releaseChannel: "stable",
    releaseProfile: "official",
  });
  assert.equal(result.schema_version, 2);
  assert.equal("release_profile" in result, false);
  assert.equal("official_release_eligible" in result, false);
  const macPackages = result.packages.filter((item) => item.kind.startsWith("macos-"));
  assert.equal(macPackages.length, 2);
  assert.ok(macPackages.every((item) => item.artifact_profile === "release"));
  assert.ok(macPackages.every((item) => item.developer_id_signed === true));
  assert.ok(macPackages.every((item) => item.notarized === true));
});

test("v1.1 unsigned-degraded evidence makes every trust downgrade explicit", async (t) => {
  const paths = fixture(t, {
    definitions: UNSIGNED_DEGRADED_FIXTURES,
    releaseTag: UNSIGNED_DEGRADED_TAG,
  });
  const result = await collect(paths, {
    releaseTag: UNSIGNED_DEGRADED_TAG,
    releaseChannel: "stable",
  });

  assert.equal(result.schema_version, 3);
  assert.equal(result.release_profile, "unsigned-degraded");
  assert.equal(result.release_channel, "stable");
  assert.equal(result.publication_channel, "prerelease");
  assert.equal(result.official_release_eligible, false);
  assert.equal(result.latest_eligible, false);
  assert.equal(result.packages.length, 7);
  assert.ok(
    result.packages.every((item) =>
      /-UNSIGNED-DEGRADED\.(?:exe|dmg|deb|rpm)$/u.test(item.artifact_name),
    ),
  );
  assert.ok(
    result.packages.every(
      (item) => item.artifact_profile === "unsigned-degraded",
    ),
  );

  const windows = result.packages.find(
    (item) => item.kind === "windows-x64-nsis",
  );
  assert.equal(windows.authenticode_signed, false);
  const macPackages = result.packages.filter((item) =>
    item.kind.startsWith("macos-"),
  );
  assert.equal(macPackages.length, 2);
  for (const item of macPackages) {
    assert.equal(item.app_signature, "adhoc");
    assert.equal(item.developer_id_signed, false);
    assert.equal(item.dmg_signed, false);
    assert.equal(item.notarized, false);
    assert.equal(item.stapled, false);
  }
  const linuxPackages = result.packages.filter((item) =>
    item.kind.startsWith("linux-"),
  );
  for (const item of linuxPackages) {
    assert.equal(item.package_signed, false);
    assert.equal(item.repository_signed, false);
  }
  for (const item of result.packages) {
    for (const key of [
      "authenticode_signed",
      "developer_id_signed",
      "dmg_signed",
      "notarized",
      "stapled",
      "package_signed",
      "repository_signed",
    ]) {
      if (key in item) assert.notEqual(item[key], true, `${item.kind}.${key}`);
    }
  }
});

test("v1.1 stable evidence rejects an official profile instead of inferring trust", async (t) => {
  const paths = fixture(t, {
    definitions: UNSIGNED_DEGRADED_FIXTURES,
    releaseTag: UNSIGNED_DEGRADED_TAG,
  });
  await assert.rejects(
    () =>
      collect(paths, {
        releaseTag: UNSIGNED_DEGRADED_TAG,
        releaseChannel: "stable",
        releaseProfile: "official",
      }),
    /unsigned-degraded release contract/u,
  );
});

test("native evidence CLI accepts unsigned-degraded as its final profile argument", (t) => {
  const paths = fixture(t, {
    definitions: UNSIGNED_DEGRADED_FIXTURES,
    releaseTag: UNSIGNED_DEGRADED_TAG,
  });
  const output = join(paths.root, "PACKAGE-LIFECYCLE-UNSIGNED-DEGRADED.json");
  const result = spawnSync(
    process.execPath,
    [
      fileURLToPath(new URL("./native-package-evidence.mjs", import.meta.url)),
      "aggregate",
      paths.assetsRoot,
      paths.artifactsRoot,
      paths.checksumFile,
      COMMIT,
      UNSIGNED_DEGRADED_TAG,
      "stable",
      output,
      "unsigned-degraded",
    ],
    { encoding: "utf8" },
  );
  assert.equal(result.status, 0, result.stderr);
  const evidence = JSON.parse(readFileSync(output, "utf8"));
  assert.equal(evidence.release_profile, "unsigned-degraded");
  assert.equal(evidence.official_release_eligible, false);
  assert.ok(
    evidence.packages.every((item) =>
      item.artifact_name.includes("-UNSIGNED-DEGRADED."),
    ),
  );
});

test("unsigned-degraded native evidence supports synchronized v1.1 RC installers", async (t) => {
  const prerelease = fixture(t, {
    definitions: UNSIGNED_DEGRADED_RC_FIXTURES,
    releaseTag: UNSIGNED_DEGRADED_RC_TAG,
  });
  const evidence = await collect(prerelease, {
    releaseTag: UNSIGNED_DEGRADED_RC_TAG,
    releaseChannel: "prerelease",
    releaseProfile: "unsigned-degraded",
  });
  assert.equal(evidence.release_tag, UNSIGNED_DEGRADED_RC_TAG);
  assert.equal(evidence.release_channel, "prerelease");
  assert.equal(evidence.release_profile, "unsigned-degraded");
  assert.ok(
    evidence.packages.every((item) =>
      item.artifact_name.includes("1.1.0-rc.2") &&
      item.artifact_name.includes("-UNSIGNED-DEGRADED."),
    ),
  );

  const stableTag = "v1.0.0";
  const definitions = FIXTURES.map((entry) => {
    const copy = [...entry];
    copy[1] = copy[1]
      .replace(VERSION, stableTag.slice(1))
      .replace("-ADHOC-NOT-NOTARIZED", "");
    return copy;
  });
  const stable = fixture(t, { definitions, releaseTag: stableTag });
  await assert.rejects(
    () =>
      collect(stable, {
        releaseTag: stableTag,
        releaseChannel: "stable",
        releaseProfile: "unsigned-degraded",
      }),
    /defined only for v1\.1\.0/u,
  );
});

test("rejects installer tampering after CHECKSUMS.md was generated", async (t) => {
  const paths = fixture(t);
  writeFileSync(join(paths.assetsRoot, FIXTURES[0][1]), "tampered installer");
  await assert.rejects(() => collect(paths), /CHECKSUMS\.md does not match/u);
});

test("rejects an installer that matches CHECKSUMS but not lifecycle evidence", async (t) => {
  const paths = fixture(t);
  const artifactName = FIXTURES[0][1];
  const artifactPath = join(paths.assetsRoot, artifactName);
  const original = readFileSync(artifactPath, "utf8");
  const replacement = original.replace("windows", "wind0ws");
  assert.equal(Buffer.byteLength(replacement), Buffer.byteLength(original));
  writeFileSync(artifactPath, replacement);
  const checksums = readFileSync(paths.checksumFile, "utf8").replace(
    sha256(original),
    sha256(replacement),
  );
  writeFileSync(paths.checksumFile, checksums);

  await assert.rejects(
    () => collect(paths),
    /does not match the artifact verified by the lifecycle run/u,
  );
});

test("rejects lifecycle evidence that omits the orphan-process proof", async (t) => {
  const paths = fixture(t);
  const reportPath = join(paths.artifactsRoot, FIXTURES[0][2], FIXTURES[0][3], "result.json");
  const report = JSON.parse(readFileSync(reportPath, "utf8"));
  report.checks.no_orphan_processes = false;
  writeFileSync(reportPath, `${JSON.stringify(report, null, 2)}\n`);
  await assert.rejects(() => collect(paths), /no_orphan_processes/u);
});

test("rejects lifecycle evidence without the preinstall artifact seal", async (t) => {
  const paths = fixture(t);
  const reportPath = join(paths.artifactsRoot, FIXTURES[0][2], FIXTURES[0][3], "result.json");
  const report = JSON.parse(readFileSync(reportPath, "utf8"));
  delete report.artifact_preinstall_seal;
  report.checks.artifact_matches_preinstall_seal = false;
  writeFileSync(reportPath, `${JSON.stringify(report, null, 2)}\n`);
  await assert.rejects(
    () => collect(paths),
    /artifact_preinstall_seal|artifact_matches_preinstall_seal/u,
  );
});

test("rejects a lifecycle report replayed from another tag or commit", async (t) => {
  const paths = fixture(t);
  const reportPath = join(paths.artifactsRoot, FIXTURES[0][2], FIXTURES[0][3], "result.json");
  const report = JSON.parse(readFileSync(reportPath, "utf8"));
  report.release_ref = "v1.0.0-rc.6";
  report.source_commit = "b".repeat(40);
  writeFileSync(reportPath, `${JSON.stringify(report, null, 2)}\n`);
  await assert.rejects(
    () => collect(paths),
    /source_commit.*expected|release_ref.*expected/u,
  );
});

test("rejects duplicate or unexpected native lifecycle results", async (t) => {
  const paths = fixture(t);
  const extra = join(
    paths.artifactsRoot,
    "linux-x64-lifecycle-diagnostics-2",
    "suxiaoyou-desktop-lifecycle-linux-deb",
  );
  mkdirSync(extra, { recursive: true });
  writeFileSync(join(extra, "result.json"), `${JSON.stringify(lifecycle("linux", 99))}\n`);
  await assert.rejects(() => collect(paths), /expected one lifecycle result/u);
});

test("rejects DEB and RPM evidence that differs beyond the Tauri bundle marker", async (t) => {
  const paths = fixture(t);
  const reportPath = join(paths.artifactsRoot, FIXTURES[3][2], FIXTURES[3][3], "result.json");
  const report = JSON.parse(readFileSync(reportPath, "utf8"));
  report.executable_unpatched_sha256 = "f".repeat(64);
  writeFileSync(reportPath, `${JSON.stringify(report, null, 2)}\n`);
  await assert.rejects(
    () => collect(paths),
    /identity differs after restoring the Tauri bundle marker/u,
  );
});
