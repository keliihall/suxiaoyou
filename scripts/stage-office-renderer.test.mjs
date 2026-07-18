import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import {
  chmodSync,
  lstatSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  readdirSync,
  rmSync,
  symlinkSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, test } from "node:test";

import {
  OFFICE_RENDERER_LOCK_FILENAME,
  OFFICE_RENDERER_PAYLOAD_CONTRACT,
  OFFICE_RENDERER_STAGE_FILENAME,
  OfficeRendererStageError,
  stageOfficeRenderer,
} from "./stage-office-renderer.mjs";

const temporaryDirectories = [];

afterEach(() => {
  for (const directory of temporaryDirectories.splice(0)) {
    rmSync(directory, { recursive: true, force: true });
  }
});

function sha256(bytes) {
  return createHash("sha256").update(bytes).digest("hex");
}

function mode(path) {
  return lstatSync(path).mode & 0o777;
}

function payloadTreeSha256(files) {
  const digest = createHash("sha256");
  for (const file of files) {
    digest.update(
      `${JSON.stringify({
        mode: file.mode,
        path: file.path,
        sha256: file.sha256,
        size: file.size,
      })}\n`,
    );
  }
  return digest.digest("hex");
}

function deploymentTreeSha256(files) {
  return payloadTreeSha256(
    files.filter((file) => file.path !== "office-renderer-attestation.json"),
  );
}

function rendererFixture(target = "linux-x64") {
  const root = mkdtempSync(join(tmpdir(), "office-renderer-stage-"));
  temporaryDirectories.push(root);
  const source = join(root, "source");
  const payload = join(source, "payload", target);
  mkdirSync(join(payload, "bin"), { recursive: true, mode: 0o755 });
  mkdirSync(join(payload, "fonts"), { mode: 0o755 });
  mkdirSync(join(payload, "probe"), { mode: 0o755 });
  chmodSync(payload, 0o755);
  chmodSync(join(payload, "bin"), 0o755);
  chmodSync(join(payload, "fonts"), 0o755);
  chmodSync(join(payload, "probe"), 0o755);
  const executableSuffix = target.startsWith("windows-") ? ".exe" : "";
  const pdftoppmPath = `bin/pdftoppm${executableSuffix}`;
  const sofficePath = `bin/soffice${executableSuffix}`;
  const launcherPath = `bin/suxiaoyou-office-sandbox-launcher${executableSuffix}`;
  const sandboxProbePath = `bin/suxiaoyou-office-sandbox-probe${executableSuffix}`;
  const executableContent = new Map([
    [pdftoppmPath, Buffer.from("signed-pdftoppm-binary")],
    [sofficePath, Buffer.from("signed-soffice-binary")],
    [launcherPath, Buffer.from("signed-native-sandbox-launcher")],
    [sandboxProbePath, Buffer.from("signed-native-sandbox-probe")],
  ]);
  const dependencyFiles = [...executableContent]
    .map(([path, bytes]) => ({
      dependencies: [],
      kind: "executable",
      path,
      sha256: sha256(bytes),
      size: bytes.length,
    }))
    .sort((left, right) => left.path.localeCompare(right.path));
  const dependencyBytes = Buffer.from(
    `${JSON.stringify({
      files: dependencyFiles,
      platform_target: target,
      schema_version: 1,
    })}\n`,
  );
  const family = target.split("-", 1)[0];
  const sandboxContracts = {
    darwin: {
      contractId: "suxiaoyou.office-sandbox.macos-app-sandbox-xpc.v1",
      capabilities: [
        "app_sandbox",
        "host_filesystem_read_only",
        "network_denied",
        "private_input_read_only",
        "private_output_write_only",
        "process_tree_contained",
        "xpc_service",
      ],
    },
    linux: {
      contractId: "suxiaoyou.office-sandbox.linux-namespaces-seccomp-cgroup.v1",
      capabilities: [
        "cgroup",
        "host_filesystem_read_only",
        "mount_namespace",
        "network_denied",
        "network_namespace",
        "private_input_read_only",
        "private_output_write_only",
        "process_tree_contained",
        "seccomp",
        "user_namespace",
      ],
    },
    windows: {
      contractId: "suxiaoyou.office-sandbox.windows-appcontainer-restricted-token.v1",
      capabilities: [
        "app_container",
        "host_filesystem_read_only",
        "kill_on_close_job",
        "network_denied",
        "private_input_read_only",
        "private_output_write_only",
        "process_tree_contained",
        "restricted_token",
      ],
    },
  };
  const sandboxContract = sandboxContracts[family];
  const sandboxBytes = Buffer.from(
    `${JSON.stringify({
      capabilities: Object.fromEntries(
        sandboxContract.capabilities.map((name) => [name, true]),
      ),
      contract_id: sandboxContract.contractId,
      launcher_path: launcherPath,
      platform_target: target,
      schema_version: 1,
    })}\n`,
  );
  const probeSourceBytes = Buffer.from("fixture-canonical-probe-docx");
  const probeManifestBytes = Buffer.from(
    `${JSON.stringify({
      dpi: 144,
      page_count: 1,
      pages: [
        {
          height_px: 2200,
          page_number: 1,
          pixel_sha256: sha256("fixture-probe-rgba"),
          width_px: 1700,
        },
      ],
      schema_version: 1,
      source_sha256: sha256(probeSourceBytes),
    })}\n`,
  );
  const content = new Map([
    ...executableContent,
    ["dependency-manifest.json", dependencyBytes],
    ["font-manifest.json", Buffer.from('{"fonts":["CJK.ttf"]}\n')],
    ["fonts/CJK.ttf", Buffer.from("release-reviewed-cjk-font")],
    ["license-manifest.json", Buffer.from('{"licenses":["OFL-1.1"]}\n')],
    ["probe/authoritative-renderer-probe.docx", probeSourceBytes],
    ["probe/authoritative-renderer-probe.json", probeManifestBytes],
    ["sandbox-manifest.json", sandboxBytes],
  ]);
  const deploymentFiles = [...content]
    .map(([path, bytes]) => ({
      mode: path.startsWith("bin/") ? 0o755 : 0o644,
      path,
      sha256: sha256(bytes),
      size: bytes.length,
    }))
    .sort((left, right) => left.path.localeCompare(right.path));
  const attestationBytes = Buffer.from(
    JSON.stringify({
      schema_version: 2,
      app_version: "1.1.0",
      release_commit: "a".repeat(40),
      platform_target: target,
      base_renderer_id: "libreoffice-pdf-png",
      base_renderer_version: "fixture-v1",
      font_digest: sha256("font-environment"),
      components: {
        "bundle-tree": deploymentTreeSha256(deploymentFiles),
        "dependency-manifest": sha256(dependencyBytes),
        "font-manifest": sha256(content.get("font-manifest.json")),
        "license-manifest": sha256(content.get("license-manifest.json")),
        pdftoppm: sha256(content.get(pdftoppmPath)),
        "sandbox-manifest": sha256(content.get("sandbox-manifest.json")),
        soffice: sha256(content.get(sofficePath)),
      },
      signature: Buffer.alloc(64, 1).toString("base64"),
    }),
  );
  content.set("office-renderer-attestation.json", attestationBytes);
  for (const [relativePath, bytes] of content) {
    const path = join(payload, ...relativePath.split("/"));
    writeFileSync(path, bytes, { mode: relativePath.startsWith("bin/") ? 0o755 : 0o644 });
    chmodSync(path, relativePath.startsWith("bin/") ? 0o755 : 0o644);
  }
  const directories = ["bin", "fonts", "probe"].map((path) => ({
    mode: mode(join(payload, path)),
    path,
  }));
  const files = [...content]
    .map(([path, bytes]) => ({
      mode: mode(join(payload, ...path.split("/"))),
      path,
      sha256: sha256(bytes),
      size: bytes.length,
    }))
    .sort((left, right) => left.path.localeCompare(right.path));
  const lock = {
    schema_version: 1,
    platform_target: target,
    payload_contract: OFFICE_RENDERER_PAYLOAD_CONTRACT,
    payload_root_mode: mode(payload),
    directories,
    files,
    payload_tree_sha256: payloadTreeSha256(files),
  };
  const lockPath = join(source, OFFICE_RENDERER_LOCK_FILENAME);

  function writeLock(next = lock) {
    const bytes = Buffer.from(`${JSON.stringify(next)}\n`);
    writeFileSync(lockPath, bytes, { mode: 0o644 });
    chmodSync(lockPath, 0o644);
    return sha256(bytes);
  }

  return {
    destination: join(root, "stage"),
    lock,
    lockPath,
    payload,
    root,
    source,
    target,
    writeLock,
  };
}

function stage(fixture, expectedLockSha256 = fixture.writeLock()) {
  return stageOfficeRenderer({
    sourceBundle: fixture.source,
    target: fixture.target,
    expectedLockSha256,
    destination: fixture.destination,
  });
}

test("atomically stages exactly one lock-bound native renderer target", () => {
  const fixture = rendererFixture();
  const expectedLockSha256 = fixture.writeLock();
  const sourceSoffice = readFileSync(join(fixture.payload, "bin", "soffice"));

  const result = stage(fixture, expectedLockSha256);

  assert.equal(result.platform_target, fixture.target);
  assert.equal(result.lock_sha256, expectedLockSha256);
  assert.deepEqual(
    readdirSync(fixture.destination).sort(),
    [
      OFFICE_RENDERER_LOCK_FILENAME,
      OFFICE_RENDERER_STAGE_FILENAME,
      "payload",
    ].sort(),
  );
  assert.deepEqual(
    readdirSync(join(fixture.destination, "payload")),
    [fixture.target],
  );
  const stagedSoffice = join(
    fixture.destination,
    "payload",
    fixture.target,
    "bin",
    "soffice",
  );
  assert.deepEqual(readFileSync(stagedSoffice), sourceSoffice);
  assert.equal(mode(stagedSoffice), mode(join(fixture.payload, "bin", "soffice")));
  assert.equal(
    mode(join(fixture.destination, "payload", fixture.target, "bin", "suxiaoyou-office-sandbox-probe")),
    0o755,
  );
  const manifest = JSON.parse(
    readFileSync(join(fixture.destination, OFFICE_RENDERER_STAGE_FILENAME), "utf8"),
  );
  assert.equal(manifest.payload_contract, OFFICE_RENDERER_PAYLOAD_CONTRACT);
  assert.equal(manifest.payload_tree_sha256, fixture.lock.payload_tree_sha256);
  const lockedPaths = fixture.lock.files.map((entry) => entry.path);
  assert.ok(lockedPaths.includes("sandbox-manifest.json"));
  assert.ok(lockedPaths.includes("bin/suxiaoyou-office-sandbox-launcher"));
  assert.ok(lockedPaths.includes("bin/suxiaoyou-office-sandbox-probe"));
  assert.ok(lockedPaths.includes("probe/authoritative-renderer-probe.json"));
  assert.ok(lockedPaths.includes("probe/authoritative-renderer-probe.docx"));
});

test("Windows staging requires the target-specific .exe components", () => {
  const fixture = rendererFixture("windows-x64");
  const expectedLockSha256 = fixture.writeLock();

  stage(fixture, expectedLockSha256);

  const paths = fixture.lock.files.map((entry) => entry.path);
  assert.ok(paths.includes("bin/soffice.exe"));
  assert.ok(paths.includes("bin/pdftoppm.exe"));
  assert.ok(paths.includes("bin/suxiaoyou-office-sandbox-launcher.exe"));
  assert.ok(paths.includes("bin/suxiaoyou-office-sandbox-probe.exe"));
  assert.equal(paths.includes("bin/soffice"), false);
  assert.equal(paths.includes("bin/pdftoppm"), false);
});

test("rejects a release-configured lock digest mismatch", () => {
  const fixture = rendererFixture();
  fixture.writeLock();

  assert.throws(
    () => stage(fixture, "a".repeat(64)),
    /lock SHA-256 does not match the release-configured digest/u,
  );
  assert.equal(readdirSync(fixture.root).includes("stage"), false);
});

test("rejects extra platform bundles and source-root files", () => {
  const fixture = rendererFixture();
  const expectedLockSha256 = fixture.writeLock();
  mkdirSync(join(fixture.source, "payload", "darwin-x64"));

  assert.throws(
    () => stage(fixture, expectedLockSha256),
    /exactly the selected target directory/u,
  );
});

test("rejects symlinks anywhere in the selected payload", {
  skip: process.platform === "win32",
}, () => {
  const fixture = rendererFixture();
  const expectedLockSha256 = fixture.writeLock();
  symlinkSync("soffice", join(fixture.payload, "bin", "soffice-alias"));

  assert.throws(
    () => stage(fixture, expectedLockSha256),
    /symlink or special file/u,
  );
});

test("rejects locked path escape before touching the destination", () => {
  const fixture = rendererFixture();
  fixture.lock.files[0].path = "../pdftoppm";
  const expectedLockSha256 = fixture.writeLock();

  assert.throws(
    () => stage(fixture, expectedLockSha256),
    /canonical relative POSIX path/u,
  );
  assert.equal(readdirSync(fixture.root).includes("stage"), false);
});

test("rejects payload byte or mode drift from the immutable lock", () => {
  const fixture = rendererFixture();
  const expectedLockSha256 = fixture.writeLock();
  writeFileSync(join(fixture.payload, "bin", "soffice"), "changed-after-lock");

  assert.throws(
    () => stage(fixture, expectedLockSha256),
    /files, bytes, sizes, or modes do not match the lock/u,
  );
});

test("refuses to merge with or overwrite an existing staging destination", () => {
  const fixture = rendererFixture();
  const expectedLockSha256 = fixture.writeLock();
  mkdirSync(fixture.destination);
  writeFileSync(join(fixture.destination, "owned-by-another-build"), "keep");

  assert.throws(
    () => stage(fixture, expectedLockSha256),
    /refusing to merge or overwrite/u,
  );
  assert.equal(
    readFileSync(join(fixture.destination, "owned-by-another-build"), "utf8"),
    "keep",
  );
});

test("uses an exclusive claim so concurrent stagers cannot share a destination", () => {
  const fixture = rendererFixture();
  const expectedLockSha256 = fixture.writeLock();
  writeFileSync(join(fixture.root, ".stage.claim"), "another-build\n");

  assert.throws(
    () => stage(fixture, expectedLockSha256),
    /already claimed by another build/u,
  );
  assert.equal(readdirSync(fixture.root).includes("stage"), false);
});

test("rejects writable payload modes before copying final bytes", {
  skip: process.platform === "win32",
}, () => {
  const fixture = rendererFixture();
  fixture.lock.directories[0].mode = 0o777;
  chmodSync(join(fixture.payload, "bin"), 0o777);
  const expectedLockSha256 = fixture.writeLock();

  assert.throws(
    () => stage(fixture, expectedLockSha256),
    /group- or world-writable payload modes/u,
  );
});

test("requires a private build-owned atomic staging parent", {
  skip: process.platform === "win32",
}, () => {
  const fixture = rendererFixture();
  const expectedLockSha256 = fixture.writeLock();
  chmodSync(fixture.root, 0o777);

  assert.throws(
    () => stage(fixture, expectedLockSha256),
    /staging parent must not be group- or world-writable/u,
  );
});

test("requires final native signing before attestation and lock creation", () => {
  const fixture = rendererFixture("darwin-arm64");
  fixture.lock.payload_contract = "attested-before-signing";
  const expectedLockSha256 = fixture.writeLock();

  assert.throws(
    () => stage(fixture, expectedLockSha256),
    /macOS nested code signing must finish before the attestation/u,
  );
});

test("rejects an attestation for a different native platform", () => {
  const fixture = rendererFixture();
  const attestationPath = join(fixture.payload, "office-renderer-attestation.json");
  const attestation = JSON.parse(readFileSync(attestationPath, "utf8"));
  attestation.platform_target = "darwin-x64";
  const bytes = Buffer.from(
    JSON.stringify(attestation),
  );
  writeFileSync(attestationPath, bytes);
  fixture.lock.files = fixture.lock.files.map((entry) =>
    entry.path === "office-renderer-attestation.json"
      ? { ...entry, sha256: sha256(bytes), size: bytes.length }
      : entry,
  );
  fixture.lock.payload_tree_sha256 = payloadTreeSha256(fixture.lock.files);
  const expectedLockSha256 = fixture.writeLock();

  assert.throws(
    () => stage(fixture, expectedLockSha256),
    /attestation must be schema v2 for target linux-x64/u,
  );
});

test("rejects a legacy renderer attestation even when its bytes are lock-bound", () => {
  const fixture = rendererFixture();
  const attestationPath = join(fixture.payload, "office-renderer-attestation.json");
  const attestation = JSON.parse(readFileSync(attestationPath, "utf8"));
  attestation.schema_version = 1;
  const bytes = Buffer.from(JSON.stringify(attestation));
  writeFileSync(attestationPath, bytes);
  fixture.lock.files = fixture.lock.files.map((entry) =>
    entry.path === "office-renderer-attestation.json"
      ? { ...entry, sha256: sha256(bytes), size: bytes.length }
      : entry,
  );
  fixture.lock.payload_tree_sha256 = payloadTreeSha256(fixture.lock.files);
  const expectedLockSha256 = fixture.writeLock();

  assert.throws(
    () => stage(fixture, expectedLockSha256),
    /attestation must be schema v2/u,
  );
});

test("rejects a signed bundle-tree identity that does not match locked files", () => {
  const fixture = rendererFixture();
  const attestationPath = join(fixture.payload, "office-renderer-attestation.json");
  const attestation = JSON.parse(readFileSync(attestationPath, "utf8"));
  attestation.components["bundle-tree"] = "b".repeat(64);
  const bytes = Buffer.from(JSON.stringify(attestation));
  writeFileSync(attestationPath, bytes);
  fixture.lock.files = fixture.lock.files.map((entry) =>
    entry.path === "office-renderer-attestation.json"
      ? { ...entry, sha256: sha256(bytes), size: bytes.length }
      : entry,
  );
  fixture.lock.payload_tree_sha256 = payloadTreeSha256(fixture.lock.files);
  const expectedLockSha256 = fixture.writeLock();

  assert.throws(
    () => stage(fixture, expectedLockSha256),
    /attestation bundle-tree does not match the locked payload/u,
  );
});

test("requires canonical lock bytes so duplicate-key ambiguity cannot pass", () => {
  const fixture = rendererFixture();
  const canonical = `${JSON.stringify(fixture.lock)}\n`;
  const ambiguous = canonical.replace(
    '"schema_version":1,',
    '"schema_version":2,"schema_version":1,',
  );
  writeFileSync(fixture.lockPath, ambiguous);

  assert.throws(
    () => stage(fixture, sha256(ambiguous)),
    /canonical JSON bytes without duplicate keys/u,
  );
});

test("reports invalid API input as a staging contract error", () => {
  const fixture = rendererFixture();

  assert.throws(
    () =>
      stageOfficeRenderer({
        sourceBundle: "relative/source",
        target: fixture.target,
        expectedLockSha256: "a".repeat(64),
        destination: fixture.destination,
      }),
    OfficeRendererStageError,
  );
});
