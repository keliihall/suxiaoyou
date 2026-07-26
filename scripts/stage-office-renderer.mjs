#!/usr/bin/env node

// Admitted source layout (no archives, downloads, or implicit discovery):
//   <source>/office-renderer.lock.json
//   <source>/payload/<one-native-target>/...
// The lock describes the final file bytes and modes. In particular, macOS
// nested binaries/frameworks must already be code-signed before the renderer
// attestation and lock are generated; staging performs no payload transform.

import { createHash } from "node:crypto";
import {
  chmodSync,
  closeSync,
  constants,
  existsSync,
  fstatSync,
  fsyncSync,
  lstatSync,
  mkdirSync,
  mkdtempSync,
  openSync,
  readSync,
  readdirSync,
  realpathSync,
  renameSync,
  rmSync,
  statSync,
  unlinkSync,
  writeSync,
  writeFileSync,
} from "node:fs";
import { basename, dirname, isAbsolute, join, relative, resolve, sep } from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";

export const OFFICE_RENDERER_LOCK_FILENAME = "office-renderer.lock.json";
export const OFFICE_RENDERER_STAGE_FILENAME = "office-renderer-stage.json";
export const OFFICE_RENDERER_PAYLOAD_CONTRACT =
  "final-native-bytes-attested-after-signing-v1";
export const OFFICE_RENDERER_STAGE_SCHEMA_VERSION = 1;
export const SUPPORTED_OFFICE_RENDERER_TARGETS = Object.freeze([
  "darwin-arm64",
  "darwin-x64",
  "linux-arm64",
  "linux-x64",
  "windows-arm64",
  "windows-x64",
]);

const SUPPORTED_TARGET_SET = new Set(SUPPORTED_OFFICE_RENDERER_TARGETS);
const SHA256_PATTERN = /^(?!0{64}$)[0-9a-f]{64}$/u;
const MAX_LOCK_BYTES = 4 * 1024 * 1024;
const MAX_FILE_BYTES = 1024 * 1024 * 1024;
const MAX_PAYLOAD_BYTES = 8 * 1024 * 1024 * 1024;
const MAX_FILES = 100_000;
const READ_BUFFER_BYTES = 1024 * 1024;
const COMMON_REQUIRED_FILES = Object.freeze([
  "dependency-manifest.json",
  "font-manifest.json",
  "license-manifest.json",
  "office-renderer-attestation.json",
  "probe/authoritative-renderer-probe.docx",
  "probe/authoritative-renderer-probe.json",
  "sandbox-manifest.json",
]);

const SANDBOX_CONTRACTS = Object.freeze({
  darwin: Object.freeze({
    contractId: "suxiaoyou.office-sandbox.macos-app-sandbox-xpc.v1",
    launcherPath: "bin/suxiaoyou-office-sandbox-launcher",
    capabilities: Object.freeze([
      "app_sandbox",
      "host_filesystem_read_only",
      "network_denied",
      "private_input_read_only",
      "private_output_write_only",
      "process_tree_contained",
      "xpc_service",
    ]),
  }),
  linux: Object.freeze({
    contractId: "suxiaoyou.office-sandbox.linux-namespaces-seccomp-cgroup.v1",
    launcherPath: "bin/suxiaoyou-office-sandbox-launcher",
    capabilities: Object.freeze([
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
    ]),
  }),
  windows: Object.freeze({
    contractId: "suxiaoyou.office-sandbox.windows-appcontainer-restricted-token.v1",
    launcherPath: "bin/suxiaoyou-office-sandbox-launcher.exe",
    capabilities: Object.freeze([
      "app_container",
      "host_filesystem_read_only",
      "kill_on_close_job",
      "network_denied",
      "private_input_read_only",
      "private_output_write_only",
      "process_tree_contained",
      "restricted_token",
    ]),
  }),
});

function sandboxContract(target) {
  return SANDBOX_CONTRACTS[target.split("-", 1)[0]];
}

function sandboxProbePath(target) {
  return target.startsWith("windows-")
    ? "bin/suxiaoyou-office-sandbox-probe.exe"
    : "bin/suxiaoyou-office-sandbox-probe";
}

function executablePaths(target) {
  return target.startsWith("windows-")
    ? Object.freeze({ pdftoppm: "bin/pdftoppm.exe", soffice: "bin/soffice.exe" })
    : Object.freeze({ pdftoppm: "bin/pdftoppm", soffice: "bin/soffice" });
}

export class OfficeRendererStageError extends Error {}

function fail(message) {
  throw new OfficeRendererStageError(message);
}

function sha256(bytes) {
  return createHash("sha256").update(bytes).digest("hex");
}

function exactKeys(value, expected, label) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    fail(`${label} must be an object`);
  }
  const actual = Object.keys(value);
  if (
    actual.length !== expected.length ||
    actual.some((key, index) => key !== expected[index])
  ) {
    fail(`${label} fields and order must be exactly: ${expected.join(", ")}`);
  }
}

function safeMode(value, label) {
  if (!Number.isSafeInteger(value) || value < 0 || value > 0o777) {
    fail(`${label} must be an octal permission value between 0 and 0777`);
  }
  return value;
}

function canonicalRelativePath(value, label) {
  if (
    typeof value !== "string" ||
    value.length < 1 ||
    value.length > 512 ||
    value !== value.normalize("NFC") ||
    value.startsWith("/") ||
    value.endsWith("/") ||
    value.includes("\\") ||
    /[\u0000-\u001f\u007f]/u.test(value)
  ) {
    fail(`${label} is not a canonical relative POSIX path`);
  }
  const parts = value.split("/");
  if (parts.some((part) => part === "" || part === "." || part === "..")) {
    fail(`${label} is not a canonical relative POSIX path`);
  }
  return value;
}

function canonicalAbsolutePath(value, label) {
  if (
    typeof value !== "string" ||
    !isAbsolute(value) ||
    /[\u0000-\u001f\u007f]/u.test(value)
  ) {
    fail(`${label} must be an explicit absolute path`);
  }
  return resolve(value);
}

function isWithin(parent, child) {
  const suffix = relative(parent, child);
  return suffix === "" || (!suffix.startsWith(`..${sep}`) && suffix !== "..");
}

function fileType(mode) {
  return Number(mode & BigInt(constants.S_IFMT));
}

function identityFromStat(info) {
  return {
    changed_ns: info.ctimeNs.toString(),
    device: info.dev.toString(),
    inode: info.ino.toString(),
    mode: Number(info.mode & 0o777n),
    modified_ns: info.mtimeNs.toString(),
    size: Number(info.size),
    type: fileType(info.mode),
  };
}

function sameIdentity(left, right) {
  return JSON.stringify(identityFromStat(left)) === JSON.stringify(identityFromStat(right));
}

function lstatBigInt(path, label) {
  try {
    return lstatSync(path, { bigint: true });
  } catch (error) {
    fail(`${label} is unavailable: ${error.code ?? error.message}`);
  }
}

function requireDirectory(path, label) {
  const info = lstatBigInt(path, label);
  if (fileType(info.mode) !== constants.S_IFDIR) {
    fail(`${label} must be a real directory, not a symlink or special file`);
  }
  return info;
}

function requireRegularFile(path, label) {
  const info = lstatBigInt(path, label);
  if (fileType(info.mode) !== constants.S_IFREG) {
    fail(`${label} must be a regular file, not a symlink or special file`);
  }
  return info;
}

function rejectRedirectedRoot(path, label) {
  requireDirectory(path, label);
  let canonical;
  try {
    canonical = realpathSync.native(path);
  } catch (error) {
    fail(`${label} cannot be resolved: ${error.code ?? error.message}`);
  }
  return canonical;
}

function requirePrivateStagingParent(path) {
  const info = requireDirectory(path, "renderer staging parent");
  if (process.platform !== "win32") {
    const mode = Number(info.mode & 0o777n);
    if ((mode & 0o022) !== 0) {
      fail("renderer staging parent must not be group- or world-writable");
    }
    if (typeof process.getuid === "function" && Number(info.uid) !== process.getuid()) {
      fail("renderer staging parent must be owned by the build user");
    }
  }
}

function readStableFile(
  path,
  label,
  { maxBytes = MAX_FILE_BYTES, captureBytes = false } = {},
) {
  const noFollow = constants.O_NOFOLLOW ?? 0;
  let descriptor;
  try {
    descriptor = openSync(path, constants.O_RDONLY | noFollow);
  } catch (error) {
    fail(`${label} cannot be opened without following links: ${error.code ?? error.message}`);
  }
  const chunks = captureBytes ? [] : null;
  const digest = createHash("sha256");
  let total = 0;
  let before;
  let after;
  try {
    before = fstatSync(descriptor, { bigint: true });
    if (fileType(before.mode) !== constants.S_IFREG || before.size > BigInt(maxBytes)) {
      fail(`${label} is not a bounded regular file`);
    }
    const buffer = Buffer.allocUnsafe(READ_BUFFER_BYTES);
    for (;;) {
      const count = readSync(descriptor, buffer, 0, buffer.length, null);
      if (count === 0) break;
      total += count;
      if (total > maxBytes) fail(`${label} exceeds its byte limit`);
      const chunk = buffer.subarray(0, count);
      if (captureBytes) chunks.push(Buffer.from(chunk));
      digest.update(chunk);
    }
    after = fstatSync(descriptor, { bigint: true });
  } finally {
    closeSync(descriptor);
  }
  const visible = lstatBigInt(path, label);
  if (
    !sameIdentity(before, after) ||
    !sameIdentity(after, visible) ||
    total !== Number(before.size)
  ) {
    fail(`${label} changed while it was read`);
  }
  return {
    ...(captureBytes ? { bytes: Buffer.concat(chunks, total) } : {}),
    mode: Number(before.mode & 0o777n),
    sha256: digest.digest("hex"),
    size: total,
  };
}

function parseCanonicalJson(bytes, label, { newline = true } = {}) {
  let text;
  let value;
  try {
    text = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
    value = JSON.parse(text);
  } catch {
    fail(`${label} must be valid UTF-8 JSON`);
  }
  const canonical = `${JSON.stringify(value)}${newline ? "\n" : ""}`;
  if (text !== canonical) {
    fail(`${label} must use canonical JSON bytes without duplicate keys`);
  }
  return value;
}

function treeDigest(files) {
  const digest = createHash("sha256");
  for (const file of files) {
    digest.update(
      `${JSON.stringify({
        mode: file.mode,
        path: file.path,
        sha256: file.sha256,
        size: file.size,
      })}\n`,
      "utf8",
    );
  }
  return digest.digest("hex");
}

function asciiCanonicalJson(value) {
  return JSON.stringify(value).replace(/[^\x00-\x7f]/gu, (character) => {
    const codePoint = character.codePointAt(0);
    if (codePoint <= 0xffff) {
      return `\\u${codePoint.toString(16).padStart(4, "0")}`;
    }
    const offset = codePoint - 0x10000;
    const high = 0xd800 + (offset >> 10);
    const low = 0xdc00 + (offset & 0x3ff);
    return `\\u${high.toString(16).padStart(4, "0")}\\u${low
      .toString(16)
      .padStart(4, "0")}`;
  });
}

function deploymentTreeDigest(files) {
  const digest = createHash("sha256");
  for (const file of files) {
    if (file.path === "office-renderer-attestation.json") continue;
    digest.update(
      `${asciiCanonicalJson({
        mode: file.mode,
        path: file.path,
        sha256: file.sha256,
        size: file.size,
      })}\n`,
      "ascii",
    );
  }
  return digest.digest("hex");
}

function validateLock(value, expectedTarget) {
  exactKeys(
    value,
    [
      "schema_version",
      "platform_target",
      "payload_contract",
      "payload_root_mode",
      "directories",
      "files",
      "payload_tree_sha256",
    ],
    "renderer lock",
  );
  if (value.schema_version !== OFFICE_RENDERER_STAGE_SCHEMA_VERSION) {
    fail(`renderer lock schema_version must be ${OFFICE_RENDERER_STAGE_SCHEMA_VERSION}`);
  }
  if (value.platform_target !== expectedTarget) {
    fail(`renderer lock platform_target must be ${expectedTarget}`);
  }
  if (value.payload_contract !== OFFICE_RENDERER_PAYLOAD_CONTRACT) {
    fail(
      `renderer lock payload_contract must be ${OFFICE_RENDERER_PAYLOAD_CONTRACT}; ` +
        "macOS nested code signing must finish before the attestation and lock are created",
    );
  }
  const payloadRootMode = safeMode(value.payload_root_mode, "payload_root_mode");
  if (!Array.isArray(value.directories)) fail("renderer lock directories must be an array");
  if (!Array.isArray(value.files) || value.files.length < 1 || value.files.length > MAX_FILES) {
    fail(`renderer lock files must contain between 1 and ${MAX_FILES} entries`);
  }

  const directories = value.directories.map((entry, index) => {
    const label = `renderer lock directories[${index}]`;
    exactKeys(entry, ["mode", "path"], label);
    return {
      mode: safeMode(entry.mode, `${label}.mode`),
      path: canonicalRelativePath(entry.path, `${label}.path`),
    };
  });
  const files = value.files.map((entry, index) => {
    const label = `renderer lock files[${index}]`;
    exactKeys(entry, ["mode", "path", "sha256", "size"], label);
    const path = canonicalRelativePath(entry.path, `${label}.path`);
    if (!SHA256_PATTERN.test(entry.sha256)) fail(`${label}.sha256 is invalid`);
    if (!Number.isSafeInteger(entry.size) || entry.size < 0 || entry.size > MAX_FILE_BYTES) {
      fail(`${label}.size is invalid`);
    }
    return {
      mode: safeMode(entry.mode, `${label}.mode`),
      path,
      sha256: entry.sha256,
      size: entry.size,
    };
  });

  for (const [label, entries] of [
    ["directory", directories],
    ["file", files],
  ]) {
    const paths = entries.map((entry) => entry.path);
    const sorted = [...paths].sort();
    if (paths.some((path, index) => path !== sorted[index]) || new Set(paths).size !== paths.length) {
      fail(`renderer lock ${label} paths must be unique and lexically sorted`);
    }
  }
  const directoryPaths = new Set(directories.map((entry) => entry.path));
  const filePaths = new Set(files.map((entry) => entry.path));
  for (const path of filePaths) {
    if (directoryPaths.has(path)) fail(`renderer lock path is both a file and directory: ${path}`);
    const parts = path.split("/");
    for (let index = 1; index < parts.length; index += 1) {
      const parent = parts.slice(0, index).join("/");
      if (!directoryPaths.has(parent)) fail(`renderer lock omits parent directory ${parent}`);
    }
  }
  for (const path of directoryPaths) {
    const parts = path.split("/");
    for (let index = 1; index < parts.length; index += 1) {
      const parent = parts.slice(0, index).join("/");
      if (!directoryPaths.has(parent)) fail(`renderer lock omits parent directory ${parent}`);
    }
  }
  const executables = executablePaths(expectedTarget);
  const nativeSandbox = sandboxContract(expectedTarget);
  const nativeSandboxProbe = sandboxProbePath(expectedTarget);
  for (const required of [
    ...COMMON_REQUIRED_FILES,
    executables.pdftoppm,
    executables.soffice,
    nativeSandbox.launcherPath,
    nativeSandboxProbe,
  ]) {
    if (!filePaths.has(required)) fail(`renderer lock omits required payload file ${required}`);
  }
  if (![...filePaths].some((path) => /^fonts\/.+\.(?:otf|ttc|ttf)$/iu.test(path))) {
    fail("renderer lock must contain at least one bundled Office font");
  }
  if (files.find((entry) => entry.path === executables.soffice).size === 0) {
    fail(`renderer lock ${executables.soffice} must not be empty`);
  }
  if (files.find((entry) => entry.path === executables.pdftoppm).size === 0) {
    fail(`renderer lock ${executables.pdftoppm} must not be empty`);
  }
  const totalBytes = files.reduce((sum, entry) => sum + entry.size, 0);
  if (!Number.isSafeInteger(totalBytes) || totalBytes > MAX_PAYLOAD_BYTES) {
    fail("renderer lock payload exceeds its total byte limit");
  }
  if (!SHA256_PATTERN.test(value.payload_tree_sha256)) {
    fail("renderer lock payload_tree_sha256 is invalid");
  }
  const calculatedTreeDigest = treeDigest(files);
  if (calculatedTreeDigest !== value.payload_tree_sha256) {
    fail("renderer lock payload_tree_sha256 does not match its file identities");
  }
  if (!expectedTarget.startsWith("windows-")) {
    const unsafeDirectory = [
      { mode: payloadRootMode, path: "." },
      ...directories,
    ].find((entry) => (entry.mode & 0o022) !== 0);
    const unsafeFile = files.find((entry) => (entry.mode & 0o022) !== 0);
    if (unsafeDirectory || unsafeFile) {
      fail("renderer lock contains group- or world-writable payload modes");
    }
    for (const executable of [
      executables.soffice,
      executables.pdftoppm,
      nativeSandbox.launcherPath,
      nativeSandboxProbe,
    ]) {
      if ((files.find((entry) => entry.path === executable).mode & 0o111) === 0) {
        fail(`renderer lock ${executable} is not executable`);
      }
    }
  }
  return {
    directories,
    files,
    payload_root_mode: payloadRootMode,
    payload_tree_sha256: calculatedTreeDigest,
    total_bytes: totalBytes,
  };
}

function validateEntryName(name, label) {
  if (
    typeof name !== "string" ||
    name.length < 1 ||
    name !== name.normalize("NFC") ||
    name === "." ||
    name === ".." ||
    name.includes("/") ||
    name.includes("\\") ||
    /[\u0000-\u001f\u007f]/u.test(name)
  ) {
    fail(`${label} contains a non-canonical filesystem entry`);
  }
}

function inventoryPayload(root) {
  const rootInfo = requireDirectory(root, "renderer payload root");
  const directories = [];
  const files = [];
  const pending = [{ absolute: root, relative: "" }];
  while (pending.length > 0) {
    const current = pending.pop();
    let names;
    try {
      names = readdirSync(current.absolute).sort();
    } catch (error) {
      fail(`renderer payload directory is unreadable: ${error.code ?? error.message}`);
    }
    for (const name of names) {
      validateEntryName(name, "renderer payload");
      const relativePath = current.relative ? `${current.relative}/${name}` : name;
      const absolutePath = join(current.absolute, name);
      const info = lstatBigInt(absolutePath, `renderer payload ${relativePath}`);
      if (fileType(info.mode) === constants.S_IFDIR) {
        directories.push({
          identity: identityFromStat(info),
          mode: Number(info.mode & 0o777n),
          path: relativePath,
        });
        pending.push({ absolute: absolutePath, relative: relativePath });
      } else if (fileType(info.mode) === constants.S_IFREG) {
        const identity = readStableFile(absolutePath, `renderer payload ${relativePath}`);
        files.push({
          identity: identityFromStat(info),
          mode: identity.mode,
          path: relativePath,
          sha256: identity.sha256,
          size: identity.size,
        });
      } else {
        fail(`renderer payload ${relativePath} is a symlink or special file`);
      }
      if (files.length > MAX_FILES) fail("renderer payload contains too many files");
    }
  }
  directories.sort((left, right) => (left.path < right.path ? -1 : left.path > right.path ? 1 : 0));
  files.sort((left, right) => (left.path < right.path ? -1 : left.path > right.path ? 1 : 0));
  return {
    directories,
    files,
    root_identity: identityFromStat(rootInfo),
    root_mode: Number(rootInfo.mode & 0o777n),
  };
}

function comparableInventory(inventory) {
  return JSON.stringify({
    directories: inventory.directories,
    files: inventory.files.map((entry) => ({
      identity: entry.identity,
      mode: entry.mode,
      path: entry.path,
      sha256: entry.sha256,
      size: entry.size,
    })),
    root_identity: inventory.root_identity,
    root_mode: inventory.root_mode,
  });
}

function verifyInventoryMatchesLock(inventory, lock) {
  if (inventory.root_mode !== lock.payload_root_mode) {
    fail("renderer payload root mode does not match the lock");
  }
  const actualDirectories = inventory.directories.map(({ mode, path }) => ({ mode, path }));
  if (JSON.stringify(actualDirectories) !== JSON.stringify(lock.directories)) {
    fail("renderer payload directories or modes do not match the lock");
  }
  const actualFiles = inventory.files.map(({ mode, path, sha256: digest, size }) => ({
    mode,
    path,
    sha256: digest,
    size,
  }));
  if (JSON.stringify(actualFiles) !== JSON.stringify(lock.files)) {
    fail("renderer payload files, bytes, sizes, or modes do not match the lock");
  }
}

function ensureV2ReleaseManifests(payloadRoot, expectedTarget, lock) {
  const dependency = readStableFile(
    join(payloadRoot, "dependency-manifest.json"),
    "renderer dependency manifest",
    { maxBytes: 4 * 1024 * 1024, captureBytes: true },
  );
  const dependencyValue = parseCanonicalJson(
    dependency.bytes,
    "renderer dependency manifest",
  );
  exactKeys(
    dependencyValue,
    ["files", "platform_target", "schema_version"],
    "renderer dependency manifest",
  );
  if (
    dependencyValue.schema_version !== 1 ||
    dependencyValue.platform_target !== expectedTarget ||
    !Array.isArray(dependencyValue.files)
  ) {
    fail("renderer dependency manifest schema or target is invalid");
  }
  const lockedFiles = new Map(lock.files.map((entry) => [entry.path, entry]));
  const executables = executablePaths(expectedTarget);
  const nativeSandbox = sandboxContract(expectedTarget);
  const nativeSandboxProbe = sandboxProbePath(expectedTarget);
  const dependencyPaths = [];
  for (const [index, record] of dependencyValue.files.entries()) {
    const label = `renderer dependency manifest files[${index}]`;
    exactKeys(record, ["dependencies", "kind", "path", "sha256", "size"], label);
    canonicalRelativePath(record.path, `${label}.path`);
    if (
      !["executable", "library"].includes(record.kind) ||
      !Array.isArray(record.dependencies) ||
      !SHA256_PATTERN.test(record.sha256) ||
      !Number.isSafeInteger(record.size) ||
      record.size < 1
    ) {
      fail(`${label} is invalid`);
    }
    dependencyPaths.push(record.path);
  }
  if (
    JSON.stringify(dependencyPaths) !== JSON.stringify([...dependencyPaths].sort()) ||
    new Set(dependencyPaths).size !== dependencyPaths.length
  ) {
    fail("renderer dependency manifest file paths are not canonical");
  }
  for (const path of [
    executables.pdftoppm,
    executables.soffice,
    nativeSandbox.launcherPath,
    nativeSandboxProbe,
  ]) {
    const record = dependencyValue.files.find((entry) => entry.path === path);
    const locked = lockedFiles.get(path);
    if (
      !record ||
      record.kind !== "executable" ||
      !locked ||
      record.sha256 !== locked.sha256 ||
      record.size !== locked.size
    ) {
      fail(`renderer dependency manifest does not bind executable ${path}`);
    }
  }

  const sandbox = readStableFile(
    join(payloadRoot, "sandbox-manifest.json"),
    "renderer sandbox manifest",
    { maxBytes: 64 * 1024, captureBytes: true },
  );
  const sandboxValue = parseCanonicalJson(
    sandbox.bytes,
    "renderer sandbox manifest",
  );
  exactKeys(
    sandboxValue,
    [
      "capabilities",
      "contract_id",
      "launcher_path",
      "platform_target",
      "schema_version",
    ],
    "renderer sandbox manifest",
  );
  const declaredCapabilities = sandboxValue.capabilities;
  if (
    sandboxValue.schema_version !== 1 ||
    sandboxValue.platform_target !== expectedTarget ||
    sandboxValue.contract_id !== nativeSandbox.contractId ||
    sandboxValue.launcher_path !== nativeSandbox.launcherPath ||
    !declaredCapabilities ||
    typeof declaredCapabilities !== "object" ||
    Array.isArray(declaredCapabilities) ||
    JSON.stringify(Object.keys(declaredCapabilities)) !==
      JSON.stringify(nativeSandbox.capabilities) ||
    Object.values(declaredCapabilities).some((value) => value !== true)
  ) {
    fail("renderer sandbox manifest contract is invalid");
  }

  const probeManifest = readStableFile(
    join(payloadRoot, "probe", "authoritative-renderer-probe.json"),
    "renderer execution probe manifest",
    { maxBytes: 64 * 1024, captureBytes: true },
  );
  const probeValue = parseCanonicalJson(
    probeManifest.bytes,
    "renderer execution probe manifest",
  );
  exactKeys(
    probeValue,
    ["dpi", "page_count", "pages", "schema_version", "source_sha256"],
    "renderer execution probe manifest",
  );
  if (
    probeValue.schema_version !== 1 ||
    probeValue.dpi !== 144 ||
    !Number.isSafeInteger(probeValue.page_count) ||
    probeValue.page_count < 1 ||
    probeValue.page_count > 32 ||
    !SHA256_PATTERN.test(probeValue.source_sha256) ||
    !Array.isArray(probeValue.pages) ||
    probeValue.pages.length < 1 ||
    probeValue.pages.length > 32 ||
    probeValue.page_count !== probeValue.pages.length
  ) {
    fail("renderer execution probe manifest contract is invalid");
  }
  for (const [index, page] of probeValue.pages.entries()) {
    exactKeys(
      page,
      ["height_px", "page_number", "pixel_sha256", "width_px"],
      `renderer execution probe page ${index}`,
    );
    if (
      page.page_number !== index + 1 ||
      !Number.isSafeInteger(page.width_px) ||
      page.width_px < 1 ||
      page.width_px > 100_000 ||
      !Number.isSafeInteger(page.height_px) ||
      page.height_px < 1 ||
      page.height_px > 100_000 ||
      page.width_px * page.height_px > 100_000_000 ||
      !SHA256_PATTERN.test(page.pixel_sha256)
    ) {
      fail("renderer execution probe page contract is invalid");
    }
  }
  const probeSourcePath = "probe/authoritative-renderer-probe.docx";
  const probeSource = lockedFiles.get(probeSourcePath);
  if (
    !probeSource ||
    probeSource.size < 1 ||
    probeSource.sha256 !== probeValue.source_sha256
  ) {
    fail("renderer execution probe source does not match its manifest");
  }

  const attestation = readStableFile(
    join(payloadRoot, "office-renderer-attestation.json"),
    "renderer attestation",
    { maxBytes: 64 * 1024, captureBytes: true },
  );
  let value;
  try {
    const text = new TextDecoder("utf-8", { fatal: true }).decode(attestation.bytes);
    value = JSON.parse(text);
    if (text !== JSON.stringify(value) && text !== `${JSON.stringify(value)}\n`) {
      fail("renderer attestation must use canonical JSON bytes without duplicate keys");
    }
  } catch (error) {
    if (error instanceof OfficeRendererStageError) throw error;
    fail("renderer attestation must be canonical UTF-8 JSON");
  }
  const expectedFields = [
    "app_version",
    "base_renderer_id",
    "base_renderer_version",
    "components",
    "font_digest",
    "platform_target",
    "release_commit",
    "schema_version",
    "signature",
  ].sort();
  if (
    !value ||
    typeof value !== "object" ||
    Array.isArray(value) ||
    JSON.stringify(Object.keys(value).sort()) !== JSON.stringify(expectedFields) ||
    value.schema_version !== 2 ||
    value.platform_target !== expectedTarget
  ) {
    fail(`renderer attestation must be schema v2 for target ${expectedTarget}`);
  }
  if (
    !/^(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)$/u.test(
      value.app_version,
    ) ||
    !/^(?!0{40}$)[0-9a-f]{40}$/u.test(value.release_commit) ||
    typeof value.base_renderer_id !== "string" ||
    value.base_renderer_id.length < 1 ||
    typeof value.base_renderer_version !== "string" ||
    value.base_renderer_version.length < 1 ||
    !SHA256_PATTERN.test(value.font_digest)
  ) {
    fail("renderer attestation v2 release identity is invalid");
  }
  const components = value.components;
  const expectedComponents = [
    "bundle-tree",
    "dependency-manifest",
    "font-manifest",
    "license-manifest",
    "pdftoppm",
    "sandbox-manifest",
    "soffice",
  ];
  if (
    !components ||
    typeof components !== "object" ||
    Array.isArray(components) ||
    JSON.stringify(Object.keys(components)) !== JSON.stringify(expectedComponents) ||
    Object.values(components).some((digest) => !SHA256_PATTERN.test(digest))
  ) {
    fail("renderer attestation v2 component identity is invalid");
  }
  const componentPaths = {
    "dependency-manifest": "dependency-manifest.json",
    "font-manifest": "font-manifest.json",
    "license-manifest": "license-manifest.json",
    pdftoppm: executables.pdftoppm,
    "sandbox-manifest": "sandbox-manifest.json",
    soffice: executables.soffice,
  };
  for (const [component, path] of Object.entries(componentPaths)) {
    if (components[component] !== lockedFiles.get(path).sha256) {
      fail(`renderer attestation component ${component} does not match the lock`);
    }
  }
  if (components["dependency-manifest"] !== dependency.sha256) {
    fail("renderer dependency manifest digest does not match attestation v2");
  }
  if (components["bundle-tree"] !== deploymentTreeDigest(lock.files)) {
    fail("renderer attestation bundle-tree does not match the locked payload");
  }
  if (
    typeof value.signature !== "string" ||
    !/^[A-Za-z0-9+/]{86}==$/u.test(value.signature) ||
    Buffer.from(value.signature, "base64").length !== 64
  ) {
    fail("renderer attestation v2 signature encoding is invalid");
  }
}

function createDirectoryWithMode(path, mode) {
  mkdirSync(path, { mode });
  chmodSync(path, mode);
  const actual = statSync(path, { bigint: true });
  if (Number(actual.mode & 0o777n) !== mode) {
    fail(`staged renderer directory mode was not preserved: ${path}`);
  }
}

function copyExactFile(source, destination, expected, label) {
  const noFollow = constants.O_NOFOLLOW ?? 0;
  let sourceDescriptor;
  let destinationDescriptor;
  try {
    sourceDescriptor = openSync(source, constants.O_RDONLY | noFollow);
    destinationDescriptor = openSync(
      destination,
      constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL,
      expected.mode,
    );
  } catch (error) {
    if (sourceDescriptor !== undefined) closeSync(sourceDescriptor);
    fail(`${label} cannot be copied without following links: ${error.code ?? error.message}`);
  }
  const buffer = Buffer.allocUnsafe(READ_BUFFER_BYTES);
  const digest = createHash("sha256");
  let total = 0;
  let before;
  let after;
  try {
    before = fstatSync(sourceDescriptor, { bigint: true });
    if (fileType(before.mode) !== constants.S_IFREG || before.size > BigInt(MAX_FILE_BYTES)) {
      fail(`${label} source is not a bounded regular file`);
    }
    for (;;) {
      const count = readSync(sourceDescriptor, buffer, 0, buffer.length, null);
      if (count === 0) break;
      total += count;
      if (total > MAX_FILE_BYTES) fail(`${label} exceeds its byte limit`);
      digest.update(buffer.subarray(0, count));
      let offset = 0;
      while (offset < count) {
        offset += writeSync(
          destinationDescriptor,
          buffer,
          offset,
          count - offset,
          null,
        );
      }
    }
    fsyncSync(destinationDescriptor);
    after = fstatSync(sourceDescriptor, { bigint: true });
  } finally {
    closeSync(sourceDescriptor);
    closeSync(destinationDescriptor);
  }
  const visible = lstatBigInt(source, `${label} source`);
  if (!sameIdentity(before, after) || !sameIdentity(after, visible)) {
    fail(`${label} source changed while it was copied`);
  }
  if (
    total !== expected.size ||
    total !== Number(before.size) ||
    digest.digest("hex") !== expected.sha256 ||
    Number(before.mode & 0o777n) !== expected.mode
  ) {
    fail(`${label} source does not match the locked bytes and mode`);
  }
  chmodSync(destination, expected.mode);
  const copied = readStableFile(destination, label, { maxBytes: MAX_FILE_BYTES });
  if (
    copied.mode !== expected.mode ||
    copied.size !== expected.size ||
    copied.sha256 !== expected.sha256
  ) {
    fail(`${label} did not preserve the source bytes and mode`);
  }
}

function writeCanonicalNewFile(path, value, mode = 0o600) {
  const bytes = Buffer.from(`${JSON.stringify(value)}\n`, "utf8");
  const descriptor = openSync(path, constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL, mode);
  try {
    writeFileSync(descriptor, bytes);
    fsyncSync(descriptor);
  } finally {
    closeSync(descriptor);
  }
  chmodSync(path, mode);
}

function rootEntries(path, label) {
  let entries;
  try {
    entries = readdirSync(path).sort();
  } catch (error) {
    fail(`${label} is unreadable: ${error.code ?? error.message}`);
  }
  return entries;
}

export function stageOfficeRenderer({
  sourceBundle,
  target,
  expectedLockSha256,
  destination,
}) {
  const sourceInput = canonicalAbsolutePath(sourceBundle, "renderer source bundle");
  const outputInput = canonicalAbsolutePath(destination, "renderer staging destination");
  if (!SUPPORTED_TARGET_SET.has(target)) fail(`unsupported renderer target: ${target}`);
  if (!SHA256_PATTERN.test(expectedLockSha256 ?? "")) {
    fail("expected renderer lock SHA-256 must be a non-zero lowercase digest");
  }
  const source = rejectRedirectedRoot(sourceInput, "renderer source bundle");
  const outputParent = rejectRedirectedRoot(
    dirname(outputInput),
    "renderer staging parent",
  );
  requirePrivateStagingParent(outputParent);
  const output = join(outputParent, basename(outputInput));
  if (isWithin(source, output) || isWithin(output, source)) {
    fail("renderer source and staging destination must not overlap");
  }
  try {
    lstatSync(output);
    fail("renderer staging destination already exists; refusing to merge or overwrite it");
  } catch (error) {
    if (error instanceof OfficeRendererStageError) throw error;
    if (error.code !== "ENOENT") throw error;
  }

  const expectedSourceEntries = [OFFICE_RENDERER_LOCK_FILENAME, "payload"];
  if (JSON.stringify(rootEntries(source, "renderer source bundle")) !== JSON.stringify(expectedSourceEntries)) {
    fail(
      "renderer source bundle must contain exactly office-renderer.lock.json and payload; " +
        "extra platform bundles are forbidden",
    );
  }
  const payloadContainer = join(source, "payload");
  requireDirectory(payloadContainer, "renderer payload container");
  const payloadEntries = rootEntries(payloadContainer, "renderer payload container");
  if (payloadEntries.length !== 1 || payloadEntries[0] !== target) {
    fail(`renderer payload must contain exactly the selected target directory ${target}`);
  }
  const payloadRoot = join(payloadContainer, target);
  requireDirectory(payloadRoot, "renderer selected payload");

  const lockPath = join(source, OFFICE_RENDERER_LOCK_FILENAME);
  requireRegularFile(lockPath, "renderer lock");
  const lockRead = readStableFile(lockPath, "renderer lock", {
    maxBytes: MAX_LOCK_BYTES,
    captureBytes: true,
  });
  if (lockRead.sha256 !== expectedLockSha256) {
    fail("renderer lock SHA-256 does not match the release-configured digest");
  }
  const lockValue = parseCanonicalJson(lockRead.bytes, "renderer lock");
  const lock = validateLock(lockValue, target);
  const before = inventoryPayload(payloadRoot);
  verifyInventoryMatchesLock(before, lock);
  ensureV2ReleaseManifests(payloadRoot, target, lock);

  const claimPath = join(outputParent, `.${basename(output)}.claim`);
  let claimDescriptor;
  try {
    claimDescriptor = openSync(
      claimPath,
      constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL,
      0o600,
    );
    writeFileSync(claimDescriptor, `${process.pid}\n`);
    fsyncSync(claimDescriptor);
  } catch (error) {
    if (claimDescriptor !== undefined) {
      closeSync(claimDescriptor);
      claimDescriptor = undefined;
      try {
        unlinkSync(claimPath);
      } catch {
        // The primary claim failure remains the release-blocking error.
      }
    }
    fail(
      `renderer staging destination is already claimed by another build: ${
        error.code ?? error.message
      }`,
    );
  } finally {
    if (claimDescriptor !== undefined) closeSync(claimDescriptor);
  }
  let temporary;
  try {
    temporary = mkdtempSync(join(outputParent, `.${basename(output)}.tmp-`));
    chmodSync(temporary, 0o700);
    const stagedPayloadContainer = join(temporary, "payload");
    createDirectoryWithMode(stagedPayloadContainer, 0o700);
    const stagedPayloadRoot = join(stagedPayloadContainer, target);
    createDirectoryWithMode(stagedPayloadRoot, lock.payload_root_mode);
    for (const directory of lock.directories) {
      createDirectoryWithMode(join(stagedPayloadRoot, ...directory.path.split("/")), directory.mode);
    }
    for (const file of lock.files) {
      copyExactFile(
        join(payloadRoot, ...file.path.split("/")),
        join(stagedPayloadRoot, ...file.path.split("/")),
        file,
        `staged renderer ${file.path}`,
      );
    }
    copyExactFile(
      lockPath,
      join(temporary, OFFICE_RENDERER_LOCK_FILENAME),
      lockRead,
      "staged renderer lock",
    );
    const manifest = {
      schema_version: OFFICE_RENDERER_STAGE_SCHEMA_VERSION,
      platform_target: target,
      payload_contract: OFFICE_RENDERER_PAYLOAD_CONTRACT,
      lock_sha256: expectedLockSha256,
      payload_tree_sha256: lock.payload_tree_sha256,
      payload_root_mode: lock.payload_root_mode,
      directory_count: lock.directories.length,
      file_count: lock.files.length,
      total_bytes: lock.total_bytes,
    };
    writeCanonicalNewFile(join(temporary, OFFICE_RENDERER_STAGE_FILENAME), manifest);
    const staged = inventoryPayload(stagedPayloadRoot);
    verifyInventoryMatchesLock(staged, lock);
    const stagedRootEntries = rootEntries(temporary, "renderer staging root");
    const declaredRootEntries = [
      OFFICE_RENDERER_LOCK_FILENAME,
      OFFICE_RENDERER_STAGE_FILENAME,
      "payload",
    ].sort();
    if (JSON.stringify(stagedRootEntries) !== JSON.stringify(declaredRootEntries)) {
      fail(
        `renderer staging root contains undeclared files: ${stagedRootEntries.join(", ")}`,
      );
    }
    const after = inventoryPayload(payloadRoot);
    if (comparableInventory(after) !== comparableInventory(before)) {
      fail("renderer source bundle changed during staging");
    }
    try {
      lstatSync(output);
      fail("renderer staging destination appeared during staging; refusing overwrite");
    } catch (error) {
      if (error instanceof OfficeRendererStageError) throw error;
      if (error.code !== "ENOENT") throw error;
    }
    renameSync(temporary, output);
    unlinkSync(claimPath);
    return Object.freeze({ ...manifest, destination: output });
  } catch (error) {
    if (temporary && existsSync(temporary)) {
      rmSync(temporary, { recursive: true, force: true });
    }
    try {
      unlinkSync(claimPath);
    } catch (claimError) {
      if (claimError.code !== "ENOENT") {
        throw new OfficeRendererStageError(
          `${error.message}; staging claim cleanup failed: ${claimError.message}`,
          { cause: error },
        );
      }
    }
    throw error;
  }
}

function usage() {
  return (
    "usage: node scripts/stage-office-renderer.mjs stage " +
    "<source-bundle> <target> <expected-lock-sha256> <destination>\n" +
    `targets: ${SUPPORTED_OFFICE_RENDERER_TARGETS.join(", ")}\n`
  );
}

function main(argv) {
  if (argv.length !== 5 || argv[0] !== "stage") fail(usage().trimEnd());
  const [, sourceBundle, target, expectedLockSha256, destination] = argv;
  const result = stageOfficeRenderer({
    sourceBundle,
    target,
    expectedLockSha256,
    destination,
  });
  process.stdout.write(
    `[stage-office-renderer] staged ${result.platform_target}; ` +
      `${result.file_count} immutable files; lock ${result.lock_sha256}\n`,
  );
}

if (resolve(process.argv[1] ?? "") === fileURLToPath(import.meta.url)) {
  try {
    main(process.argv.slice(2));
  } catch (error) {
    process.stderr.write(`[stage-office-renderer] ${error.message}\n`);
    process.exitCode = 1;
  }
}
