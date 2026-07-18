#!/usr/bin/env node

/**
 * Launch a packaged 苏小有 desktop executable, wait for the native shell to
 * report that its embedded backend is ready, request the real graceful Quit
 * path, and prove the observed child process tree was reaped.
 *
 * The executable exposes the control files only when release CI supplies
 * SUXIAOYOU_DESKTOP_LIFECYCLE_SMOKE_DIR. No authentication token is read or
 * written by this verifier. Release CI seals the installer/package before its
 * first install or mount. This verifier requires that size/SHA-256/commit seal,
 * checks it before launching the app, and re-hashes the artifact after exit.
 */

import { spawn, spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import {
  closeSync,
  constants,
  cpSync,
  createWriteStream,
  existsSync,
  fstatSync,
  mkdirSync,
  openSync,
  readFileSync,
  readSync,
  realpathSync,
  rmSync,
  statSync,
  writeFileSync,
} from "node:fs";
import { dirname, isAbsolute, join, posix, resolve, win32 } from "node:path";
import { setTimeout as delay } from "node:timers/promises";

import { isMainModule } from "./release-metadata.mjs";
import { resolveCheckoutCommit } from "./office-contract-evidence.mjs";

const DEFAULT_STARTUP_TIMEOUT_MS = 120_000;
const DEFAULT_SHUTDOWN_TIMEOUT_MS = 45_000;
const POLL_INTERVAL_MS = 200;
const TAURI_BUNDLE_MARKER_PREFIX = Buffer.from("__TAURI_BUNDLE_TYPE_VAR_", "ascii");
const TAURI_BUNDLE_MARKER_PLACEHOLDER = Buffer.from(
  "__TAURI_BUNDLE_TYPE_VAR_UNK",
  "ascii",
);
const TAURI_LINUX_BUNDLE_MARKERS = Object.freeze({
  deb: Buffer.from("__TAURI_BUNDLE_TYPE_VAR_DEB", "ascii"),
  rpm: Buffer.from("__TAURI_BUNDLE_TYPE_VAR_RPM", "ascii"),
});
if (
  Object.values(TAURI_LINUX_BUNDLE_MARKERS).some(
    (marker) => marker.length !== TAURI_BUNDLE_MARKER_PLACEHOLDER.length,
  )
) {
  throw new Error("Tauri bundle markers must have identical byte lengths");
}
const WINDOWS_RESERVED_PATH_SEGMENT =
  /^(?:CON|PRN|AUX|NUL|CLOCK\$|CONIN\$|CONOUT\$|COM[1-9¹²³]|LPT[1-9¹²³])(?:\.|$)/iu;
const FULL_COMMIT_PATTERN = /^(?!0{40}$)[0-9a-f]{40}$/u;

export const DESKTOP_LIFECYCLE_SCHEMA_VERSION = 3;

function normalizedTauriBundleDigest(bytes, expectedBundleType) {
  const expectedMarker = TAURI_LINUX_BUNDLE_MARKERS[expectedBundleType];
  if (!expectedMarker) {
    throw new Error(`unsupported Tauri Linux bundle type: ${String(expectedBundleType)}`);
  }
  const firstMarker = bytes.indexOf(TAURI_BUNDLE_MARKER_PREFIX);
  const secondMarker =
    firstMarker < 0
      ? -1
      : bytes.indexOf(TAURI_BUNDLE_MARKER_PREFIX, firstMarker + 1);
  if (firstMarker < 0 || secondMarker >= 0) {
    throw new Error(
      `desktop executable must contain exactly one Tauri bundle marker; found ${
        firstMarker < 0 ? 0 : "multiple"
      }`,
    );
  }
  const actualMarker = bytes.subarray(
    firstMarker,
    firstMarker + TAURI_BUNDLE_MARKER_PLACEHOLDER.length,
  );
  if (!actualMarker.equals(expectedMarker)) {
    throw new Error(
      `desktop executable Tauri bundle marker does not match ${expectedBundleType}`,
    );
  }
  const normalized = Buffer.from(bytes);
  TAURI_BUNDLE_MARKER_PLACEHOLDER.copy(normalized, firstMarker);
  return createHash("sha256").update(normalized).digest("hex");
}

function isCanonicalAbsoluteExecutablePath(value, platform) {
  if (typeof value !== "string" || value.length === 0 || /[\0\r\n]/u.test(value)) {
    return false;
  }

  const pathApi =
    platform === "win32"
      ? win32
      : platform === "darwin" || platform === "linux"
        ? posix
        : null;
  if (!pathApi || !pathApi.isAbsolute(value) || pathApi.normalize(value) !== value) {
    return false;
  }

  if (platform === "win32") {
    if (value.includes("/") || value.startsWith("\\\\?\\") || value.startsWith("\\\\.\\")) {
      return false;
    }
    const driveAbsolute = /^[A-Za-z]:\\/u.test(value);
    if (!driveAbsolute) return false;
    const pathWithoutRootPrefix = value.slice(2);
    if (/[<>:"|?*\u0000-\u001f]/u.test(pathWithoutRootPrefix)) return false;
  } else if (value.includes("\\")) {
    return false;
  }

  const parsed = pathApi.parse(value);
  if (!parsed.base || value.endsWith(pathApi.sep)) return false;
  const segments = value.slice(parsed.root.length).split(pathApi.sep);
  return segments.every(
    (segment) =>
      segment.length > 0 &&
      segment !== "." &&
      segment !== ".." &&
      (platform !== "win32" ||
        (!segment.endsWith(".") &&
          !segment.endsWith(" ") &&
          !WINDOWS_RESERVED_PATH_SEGMENT.test(segment))),
  );
}

export function validateDesktopLifecycleReport(
  value,
  { expectedPlatform, expectedCommit, expectedReleaseRef, expectedBundleType } = {},
) {
  const report = value && typeof value === "object" && !Array.isArray(value) ? value : {};
  const failures = [];
  if (report.schema_version !== DESKTOP_LIFECYCLE_SCHEMA_VERSION) {
    failures.push(`schema_version must be ${DESKTOP_LIFECYCLE_SCHEMA_VERSION}`);
  }
  if (report.status !== "ok" || report.ok !== true) {
    failures.push("status/ok does not prove a successful lifecycle run");
  }
  if (!new Set(["win32", "darwin", "linux"]).has(report.platform)) {
    failures.push(`unsupported platform ${String(report.platform ?? "missing")}`);
  }
  if (expectedPlatform && report.platform !== expectedPlatform) {
    failures.push(
      `platform is ${String(report.platform ?? "missing")}, expected ${expectedPlatform}`,
    );
  }
  const sourceCommit = String(report.source_commit ?? "").toLowerCase();
  if (!/^(?!0{40}$)[0-9a-f]{40}$/u.test(sourceCommit)) {
    failures.push("source_commit must be a full non-zero Git commit ID");
  }
  if (expectedCommit && sourceCommit !== String(expectedCommit).toLowerCase()) {
    failures.push(
      `source_commit is ${sourceCommit || "missing"}, expected ${expectedCommit}`,
    );
  }
  const releaseRef = String(report.release_ref ?? "");
  if (!releaseRef) failures.push("release_ref is missing");
  if (expectedReleaseRef && releaseRef !== expectedReleaseRef) {
    failures.push(
      `release_ref is ${releaseRef || "missing"}, expected ${expectedReleaseRef}`,
    );
  }
  if (!isCanonicalAbsoluteExecutablePath(report.executable_path, report.platform)) {
    failures.push("executable_path must be an absolute canonical path");
  }
  if (!Number.isSafeInteger(report.executable_size) || report.executable_size <= 0) {
    failures.push("executable_size must be a positive safe integer");
  }
  if (!/^(?!0{64}$)[0-9a-f]{64}$/u.test(String(report.executable_sha256 ?? ""))) {
    failures.push("executable_sha256 must be a non-zero SHA-256 digest");
  }
  if (!isCanonicalAbsoluteExecutablePath(report.artifact_path, report.platform)) {
    failures.push("artifact_path must be an absolute canonical path");
  }
  if (!Number.isSafeInteger(report.artifact_size) || report.artifact_size <= 0) {
    failures.push("artifact_size must be a positive safe integer");
  }
  if (!/^(?!0{64}$)[0-9a-f]{64}$/u.test(String(report.artifact_sha256 ?? ""))) {
    failures.push("artifact_sha256 must be a non-zero SHA-256 digest");
  }
  const preinstallSeal =
    report.artifact_preinstall_seal &&
    typeof report.artifact_preinstall_seal === "object" &&
    !Array.isArray(report.artifact_preinstall_seal)
      ? report.artifact_preinstall_seal
      : {};
  const sealedCommit = String(preinstallSeal.source_commit ?? "").toLowerCase();
  if (!FULL_COMMIT_PATTERN.test(sealedCommit)) {
    failures.push(
      "artifact_preinstall_seal.source_commit must be a full non-zero Git commit ID",
    );
  } else if (sealedCommit !== sourceCommit) {
    failures.push("artifact_preinstall_seal.source_commit must match source_commit");
  }
  if (
    !Number.isSafeInteger(preinstallSeal.artifact_size) ||
    preinstallSeal.artifact_size <= 0
  ) {
    failures.push(
      "artifact_preinstall_seal.artifact_size must be a positive safe integer",
    );
  } else if (preinstallSeal.artifact_size !== report.artifact_size) {
    failures.push("artifact_preinstall_seal.artifact_size must match artifact_size");
  }
  const sealedSha256 = String(preinstallSeal.artifact_sha256 ?? "").toLowerCase();
  if (!/^(?!0{64}$)[0-9a-f]{64}$/u.test(sealedSha256)) {
    failures.push(
      "artifact_preinstall_seal.artifact_sha256 must be a non-zero SHA-256 digest",
    );
  } else if (sealedSha256 !== report.artifact_sha256) {
    failures.push("artifact_preinstall_seal.artifact_sha256 must match artifact_sha256");
  }
  const hasBundleEvidence =
    report.tauri_bundle_type !== undefined ||
    report.executable_unpatched_sha256 !== undefined ||
    expectedBundleType !== undefined;
  if (hasBundleEvidence) {
    if (report.platform !== "linux") {
      failures.push("Tauri bundle evidence is only valid for linux reports");
    }
    if (!Object.hasOwn(TAURI_LINUX_BUNDLE_MARKERS, report.tauri_bundle_type)) {
      failures.push("tauri_bundle_type must be deb or rpm");
    }
    if (expectedBundleType && report.tauri_bundle_type !== expectedBundleType) {
      failures.push(
        `tauri_bundle_type is ${String(report.tauri_bundle_type ?? "missing")}, expected ${expectedBundleType}`,
      );
    }
    if (
      !/^(?!0{64}$)[0-9a-f]{64}$/u.test(
        String(report.executable_unpatched_sha256 ?? ""),
      )
    ) {
      failures.push(
        "executable_unpatched_sha256 must be a non-zero SHA-256 digest",
      );
    }
  }
  const startedAt = Date.parse(String(report.started_at ?? ""));
  const completedAt = Date.parse(String(report.completed_at ?? ""));
  if (
    !Number.isFinite(startedAt) ||
    !Number.isFinite(completedAt) ||
    completedAt < startedAt
  ) {
    failures.push("started_at/completed_at must be a valid ordered interval");
  }
  for (const field of ["desktopPid", "backendPid"]) {
    if (!Number.isSafeInteger(report[field]) || report[field] <= 1) {
      failures.push(`${field} must be a process ID`);
    }
  }
  if (report.desktopPid === report.backendPid) {
    failures.push("desktopPid and backendPid must differ");
  }
  const descendants = Array.isArray(report.observedDescendantPids)
    ? report.observedDescendantPids
    : [];
  if (!descendants.every((pid) => Number.isSafeInteger(pid) && pid > 1)) {
    failures.push("observedDescendantPids must contain only process IDs");
  }
  if (!descendants.includes(report.backendPid)) {
    failures.push("observedDescendantPids must include backendPid");
  }
  const exit = report.exit && typeof report.exit === "object" ? report.exit : {};
  if (exit.code !== 0 || exit.signal !== null) {
    failures.push("desktop exit must be clean");
  }
  const checks = report.checks && typeof report.checks === "object" ? report.checks : {};
  for (const field of [
    "backend_ready",
    "backend_healthy",
    "graceful_exit",
    "no_orphan_processes",
    "backend_stopped",
    "artifact_unchanged",
    "artifact_matches_preinstall_seal",
  ]) {
    if (checks[field] !== true) failures.push(`checks.${field} must be true`);
  }
  try {
    const backendUrl = new URL(report.backendUrl);
    if (
      backendUrl.protocol !== "http:" ||
      backendUrl.hostname !== "127.0.0.1" ||
      backendUrl.pathname !== "/" ||
      backendUrl.search ||
      backendUrl.hash ||
      !backendUrl.port
    ) {
      failures.push("backendUrl must be an exact loopback origin");
    }
  } catch {
    failures.push("backendUrl must be a valid URL");
  }
  return { ok: failures.length === 0, failures, report };
}

export function buildIsolatedEnvironment(workDirectory, platform, base = process.env) {
  const work = resolve(workDirectory);
  const home = join(work, "home");
  const temporary = join(work, "tmp");
  const control = join(work, "control");
  for (const directory of [home, temporary, control]) {
    mkdirSync(directory, { recursive: true });
  }

  const environment = {
    ...base,
    SUXIAOYOU_DESKTOP_LIFECYCLE_SMOKE_DIR: control,
    RUST_LOG: "info",
    HOME: home,
    TMPDIR: temporary,
    TMP: temporary,
    TEMP: temporary,
  };

  if (platform === "win32") {
    environment.USERPROFILE = home;
    environment.APPDATA = join(home, "AppData", "Roaming");
    environment.LOCALAPPDATA = join(home, "AppData", "Local");
    mkdirSync(environment.APPDATA, { recursive: true });
    mkdirSync(environment.LOCALAPPDATA, { recursive: true });
  } else {
    environment.XDG_DATA_HOME = join(home, ".local", "share");
    environment.XDG_CONFIG_HOME = join(home, ".config");
    environment.XDG_CACHE_HOME = join(home, ".cache");
    environment.XDG_STATE_HOME = join(home, ".local", "state");
    for (const key of [
      "XDG_DATA_HOME",
      "XDG_CONFIG_HOME",
      "XDG_CACHE_HOME",
      "XDG_STATE_HOME",
    ]) {
      mkdirSync(environment[key], { recursive: true });
    }
  }

  if (platform === "linux") {
    environment.GDK_BACKEND = "x11";
    environment.LIBGL_ALWAYS_SOFTWARE = "1";
    environment.WEBKIT_DISABLE_COMPOSITING_MODE = "1";
  }

  return { environment, control, home, temporary };
}

export function validateReadyMarker(marker, expectedDesktopPid) {
  if (!marker || typeof marker !== "object" || Array.isArray(marker)) {
    throw new Error("ready marker must be an object");
  }
  for (const field of ["desktopPid", "backendPid"]) {
    if (!Number.isSafeInteger(marker[field]) || marker[field] <= 1) {
      throw new Error(`ready marker ${field} must be a process ID`);
    }
  }
  if (marker.desktopPid !== expectedDesktopPid) {
    throw new Error(
      `ready marker belongs to desktop PID ${marker.desktopPid}, expected ${expectedDesktopPid}`,
    );
  }
  if (marker.backendPid === marker.desktopPid) {
    throw new Error("backend PID must differ from desktop PID");
  }
  const backendUrl = new URL(marker.backendUrl);
  if (
    backendUrl.protocol !== "http:" ||
    backendUrl.hostname !== "127.0.0.1" ||
    backendUrl.pathname !== "/" ||
    backendUrl.search ||
    backendUrl.hash ||
    !backendUrl.port
  ) {
    throw new Error(`backend URL is not an exact loopback origin: ${marker.backendUrl}`);
  }
  for (const field of ["appDataDir", "appLogDir"]) {
    if (typeof marker[field] !== "string" || !isAbsolute(marker[field])) {
      throw new Error(`ready marker ${field} must be an absolute path`);
    }
  }
  return { ...marker, backendUrl: backendUrl.origin };
}

export function descendantProcessIds(processes, rootPid) {
  const children = new Map();
  for (const process of processes) {
    if (!children.has(process.ppid)) children.set(process.ppid, []);
    children.get(process.ppid).push(process.pid);
  }
  const descendants = [];
  const pending = [...(children.get(rootPid) ?? [])];
  const seen = new Set();
  while (pending.length > 0) {
    const pid = pending.shift();
    if (seen.has(pid)) continue;
    seen.add(pid);
    descendants.push(pid);
    pending.push(...(children.get(pid) ?? []));
  }
  return descendants;
}

export function parsePosixProcessTable(output) {
  const processes = [];
  for (const line of output.split(/\r?\n/)) {
    const match = /^\s*(\d+)\s+(\d+)\s+(\S+)\s*(.*)$/.exec(line);
    if (!match) continue;
    processes.push({
      pid: Number(match[1]),
      ppid: Number(match[2]),
      state: match[3],
      name: match[4],
    });
  }
  return processes;
}

export function resolveDesktopExecutable(executable) {
  const requested = resolve(executable);
  if (!existsSync(requested)) {
    throw new Error(`desktop executable does not exist: ${requested}`);
  }

  // macOS /var is a symlink to /private/var, and Tauri intentionally rejects
  // a starting executable whose path contains any symlink ancestor. CI's
  // mktemp uses /var/folders, so launch the exact same installed binary
  // through its canonical path instead of weakening Tauri's safety check.
  return realpathSync(requested);
}

function hashStableRegularFile(
  requestedPath,
  { label, expectedBundleType, resolvePath = resolveDesktopExecutable },
) {
  if (
    expectedBundleType !== undefined &&
    !Object.hasOwn(TAURI_LINUX_BUNDLE_MARKERS, expectedBundleType)
  ) {
    throw new Error(`unsupported Tauri Linux bundle type: ${String(expectedBundleType)}`);
  }
  const path = resolvePath(requestedPath);
  const before = statSync(path, { bigint: false });
  let descriptor = -1;
  try {
    descriptor = openSync(path, constants.O_RDONLY | (constants.O_NOFOLLOW ?? 0));
    const openedBefore = fstatSync(descriptor, { bigint: false });
    if (!openedBefore.isFile() || openedBefore.size <= 0) {
      throw new Error(`${label} must be a non-empty regular file: ${path}`);
    }
    const hash = createHash("sha256");
    const buffer = Buffer.allocUnsafe(1024 * 1024);
    const chunks = expectedBundleType ? [] : null;
    let total = 0;
    while (true) {
      const count = readSync(descriptor, buffer, 0, buffer.length, null);
      if (count === 0) break;
      const chunk = buffer.subarray(0, count);
      hash.update(chunk);
      if (chunks) chunks.push(Buffer.from(chunk));
      total += count;
    }
    const openedAfter = fstatSync(descriptor, { bigint: false });
    const after = statSync(path, { bigint: false });
    for (const candidate of [openedBefore, openedAfter, after]) {
      if (
        !candidate.isFile() ||
        candidate.dev !== before.dev ||
        candidate.ino !== before.ino ||
        candidate.size !== before.size ||
        candidate.mtimeMs !== before.mtimeMs
      ) {
        throw new Error(`${label} changed while it was hashed: ${path}`);
      }
    }
    if (total !== before.size) {
      throw new Error(`${label} changed while it was hashed: ${path}`);
    }
    const evidence = {
      path,
      size: total,
      sha256: hash.digest("hex"),
    };
    if (chunks) {
      evidence.tauriBundleType = expectedBundleType;
      evidence.unpatchedSha256 = normalizedTauriBundleDigest(
        Buffer.concat(chunks, total),
        expectedBundleType,
      );
    }
    return evidence;
  } finally {
    if (descriptor >= 0) closeSync(descriptor);
  }
}

export function hashStableExecutable(executable, { expectedBundleType } = {}) {
  return hashStableRegularFile(executable, {
    label: "desktop executable",
    expectedBundleType,
  });
}

export function hashStableArtifact(artifact) {
  return hashStableRegularFile(artifact, {
    label: "release artifact",
    resolvePath(requestedPath) {
      const requested = resolve(requestedPath);
      if (!existsSync(requested)) {
        throw new Error(`release artifact does not exist: ${requested}`);
      }
      return realpathSync(requested);
    },
  });
}

function assertArtifactEvidenceMatches(expected, actual, context) {
  const mismatches = [];
  if (actual.path !== expected.path) mismatches.push("canonical path");
  if (actual.size !== expected.size) mismatches.push("size");
  if (actual.sha256 !== expected.sha256) mismatches.push("SHA-256");
  if (mismatches.length > 0) {
    throw new Error(
      `${context}: release artifact changed (${mismatches.join(", ")})`,
    );
  }
  return actual;
}

function assertArtifactMatchesPreinstallSeal(expected, actual) {
  const mismatches = [];
  if (actual.size !== expected.artifact_size) mismatches.push("size");
  if (actual.sha256 !== expected.artifact_sha256) mismatches.push("SHA-256");
  if (mismatches.length > 0) {
    throw new Error(
      `preinstall artifact seal verification failed: release artifact changed (${mismatches.join(", ")})`,
    );
  }
  return actual;
}

export function verifyLifecycleArtifactBinding({
  report,
  reportPath,
  artifact,
  expectedPlatform,
  expectedCommit,
  expectedReleaseRef,
  expectedBundleType,
}) {
  if ((report === undefined) === (reportPath === undefined)) {
    throw new Error("provide exactly one of report or reportPath");
  }
  if (!artifact) throw new Error("artifact is required");
  let reportValue = report;
  if (reportPath !== undefined) {
    try {
      reportValue = JSON.parse(readFileSync(resolve(reportPath), "utf8"));
    } catch (error) {
      throw new Error(
        `cannot read lifecycle report: ${error instanceof Error ? error.message : String(error)}`,
      );
    }
  }
  const validation = validateDesktopLifecycleReport(reportValue, {
    expectedPlatform,
    expectedCommit,
    expectedReleaseRef,
    expectedBundleType,
  });
  if (!validation.ok) {
    throw new Error(`lifecycle report is invalid: ${validation.failures.join("; ")}`);
  }
  return assertArtifactEvidenceMatches(
    {
      path: validation.report.artifact_path,
      size: validation.report.artifact_size,
      sha256: validation.report.artifact_sha256,
    },
    hashStableArtifact(artifact),
    "lifecycle artifact verification failed",
  );
}

export async function verifyDesktopLifecycle({
  executable,
  artifact,
  workDirectory,
  platform = process.platform,
  startupTimeoutMs = DEFAULT_STARTUP_TIMEOUT_MS,
  shutdownTimeoutMs = DEFAULT_SHUTDOWN_TIMEOUT_MS,
  sourceCommit = resolveCheckoutCommit(),
  releaseRef = process.env.GITHUB_REF_NAME || "local",
  expectedBundleType,
  expectedArtifactSize,
  expectedArtifactSha256,
}) {
  const startedAt = new Date().toISOString();
  const normalizedSourceCommit = String(sourceCommit ?? "").trim().toLowerCase();
  if (!FULL_COMMIT_PATTERN.test(normalizedSourceCommit)) {
    throw new Error("sourceCommit must be a full non-zero Git commit ID");
  }
  if (!artifact) throw new Error("release artifact is required");
  if (!Number.isSafeInteger(expectedArtifactSize) || expectedArtifactSize <= 0) {
    throw new Error("expectedArtifactSize must be a positive safe integer");
  }
  const normalizedExpectedArtifactSha256 = String(
    expectedArtifactSha256 ?? "",
  ).toLowerCase();
  if (!/^(?!0{64}$)[0-9a-f]{64}$/u.test(normalizedExpectedArtifactSha256)) {
    throw new Error("expectedArtifactSha256 must be a non-zero SHA-256 digest");
  }
  const preinstallSeal = {
    source_commit: normalizedSourceCommit,
    artifact_size: expectedArtifactSize,
    artifact_sha256: normalizedExpectedArtifactSha256,
  };
  const artifactEvidence = assertArtifactMatchesPreinstallSeal(
    preinstallSeal,
    hashStableArtifact(artifact),
  );
  const application = resolveDesktopExecutable(executable);
  if (expectedBundleType && platform !== "linux") {
    throw new Error("Tauri Linux bundle evidence is only valid on linux");
  }
  const executableEvidence = hashStableExecutable(application, {
    expectedBundleType,
  });
  const work = resolve(workDirectory);
  if (!Number.isSafeInteger(startupTimeoutMs) || startupTimeoutMs <= 0) {
    throw new Error("startup timeout must be a positive integer");
  }
  if (!Number.isSafeInteger(shutdownTimeoutMs) || shutdownTimeoutMs <= 0) {
    throw new Error("shutdown timeout must be a positive integer");
  }

  rmSync(work, { recursive: true, force: true });
  mkdirSync(work, { recursive: true });
  const { environment, control } = buildIsolatedEnvironment(work, platform);
  const stdoutPath = join(work, "desktop-stdout.log");
  const stderrPath = join(work, "desktop-stderr.log");
  const stdoutLog = createWriteStream(stdoutPath, { flags: "wx" });
  const stderrLog = createWriteStream(stderrPath, { flags: "wx" });
  writeFileSync(
    join(work, "launch.json"),
    `${JSON.stringify({ executable: application, platform, control }, null, 2)}\n`,
  );

  let child;
  let closed = false;
  let closeResult;
  let ready;
  let observedProcesses = [];
  let closeResolve;
  const closePromise = new Promise((resolvePromise) => {
    closeResolve = resolvePromise;
  });

  try {
    child = spawn(application, [], {
      cwd: dirname(application),
      env: environment,
      stdio: ["ignore", "pipe", "pipe"],
      windowsHide: false,
    });
    child.stdout.pipe(stdoutLog);
    child.stderr.pipe(stderrLog);
    child.once("error", (error) => {
      if (!closed) closeResolve({ spawnError: error });
    });
    child.once("close", (code, signal) => {
      closed = true;
      closeResult = { code, signal };
      closeResolve(closeResult);
    });

    ready = validateReadyMarker(
      await waitForReadyMarker(control, closePromise, startupTimeoutMs),
      child.pid,
    );
    await requireHealthyBackend(ready.backendUrl);

    const processTable = listProcesses(platform);
    observedProcesses = descendantProcessIds(processTable, child.pid);
    if (!observedProcesses.includes(ready.backendPid)) {
      throw new Error(
        `ready backend PID ${ready.backendPid} is not a descendant of desktop PID ${child.pid}`,
      );
    }
    const processByPid = new Map(processTable.map((item) => [item.pid, item]));
    writeFileSync(
      join(work, "observed-processes.json"),
      `${JSON.stringify(
        observedProcesses.map((pid) => processByPid.get(pid) ?? { pid }),
        null,
        2,
      )}\n`,
    );

    writeFileSync(join(control, "request-exit"), "graceful-exit\n", { flag: "wx" });
    const [cleanup, desktopExit] = await Promise.all([
      waitForJson(join(control, "cleanup.json"), shutdownTimeoutMs),
      withTimeout(closePromise, shutdownTimeoutMs, "desktop did not exit after graceful request"),
    ]);
    if (!cleanup.backendCleanupOk) {
      throw new Error(`desktop reported failed backend cleanup: ${cleanup.detail ?? "unknown"}`);
    }
    if (desktopExit.spawnError) {
      throw new Error(`desktop failed to launch: ${desktopExit.spawnError.message}`);
    }
    if (desktopExit.code !== 0 || desktopExit.signal !== null) {
      throw new Error(
        `desktop exit was not clean (code=${desktopExit.code}, signal=${desktopExit.signal ?? "none"})`,
      );
    }

    const pidsToReap = [...new Set([ready.backendPid, ...observedProcesses])];
    await waitForProcessesGone(pidsToReap, platform, shutdownTimeoutMs);
    await requireBackendStopped(ready.backendUrl, 10_000);
    copyApplicationLogs(ready.appLogDir, work);
    const verifiedArtifactEvidence = assertArtifactEvidenceMatches(
      artifactEvidence,
      hashStableArtifact(artifact),
      "desktop lifecycle verification failed",
    );

    const result = {
      schema_version: DESKTOP_LIFECYCLE_SCHEMA_VERSION,
      status: "ok",
      ok: true,
      platform,
      source_commit: normalizedSourceCommit,
      release_ref: releaseRef,
      executable_path: executableEvidence.path,
      executable_size: executableEvidence.size,
      executable_sha256: executableEvidence.sha256,
      artifact_path: verifiedArtifactEvidence.path,
      artifact_size: verifiedArtifactEvidence.size,
      artifact_sha256: verifiedArtifactEvidence.sha256,
      artifact_preinstall_seal: preinstallSeal,
      ...(expectedBundleType
        ? {
            tauri_bundle_type: executableEvidence.tauriBundleType,
            executable_unpatched_sha256: executableEvidence.unpatchedSha256,
          }
        : {}),
      started_at: startedAt,
      completed_at: new Date().toISOString(),
      desktopPid: child.pid,
      backendPid: ready.backendPid,
      backendUrl: ready.backendUrl,
      observedDescendantPids: observedProcesses,
      exit: desktopExit,
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
    const validation = validateDesktopLifecycleReport(result, {
      expectedPlatform: platform,
      expectedCommit: normalizedSourceCommit,
      expectedReleaseRef: releaseRef,
      expectedBundleType,
    });
    if (!validation.ok) {
      throw new Error(
        `generated lifecycle report is invalid: ${validation.failures.join("; ")}`,
      );
    }
    writeFileSync(join(work, "result.json"), `${JSON.stringify(result, null, 2)}\n`);
    console.log(
      `[verify-desktop-lifecycle] ready=${ready.backendUrl}, desktop=${child.pid}, ` +
        `backend=${ready.backendPid}, descendants=${observedProcesses.length}, cleanup=passed`,
    );
    return result;
  } catch (error) {
    let failure = error;
    try {
      assertArtifactEvidenceMatches(
        artifactEvidence,
        hashStableArtifact(artifact),
        "desktop lifecycle verification failed",
      );
    } catch (artifactError) {
      const artifactMessage =
        artifactError instanceof Error ? artifactError.message : String(artifactError);
      const originalMessage = error instanceof Error ? error.message : String(error);
      if (!originalMessage.includes(artifactMessage)) {
        failure = new Error(`${artifactMessage}; lifecycle error: ${originalMessage}`);
      }
    }
    writeFileSync(
      join(work, "failure.json"),
      `${JSON.stringify(
        {
          schema_version: DESKTOP_LIFECYCLE_SCHEMA_VERSION,
          status: "failed",
          ok: false,
          platform,
          source_commit: normalizedSourceCommit,
          release_ref: releaseRef,
          executable_path: executableEvidence.path,
          executable_size: executableEvidence.size,
          executable_sha256: executableEvidence.sha256,
          artifact_path: artifactEvidence.path,
          artifact_size: artifactEvidence.size,
          artifact_sha256: artifactEvidence.sha256,
          artifact_preinstall_seal: preinstallSeal,
          ...(expectedBundleType
            ? {
                tauri_bundle_type: executableEvidence.tauriBundleType,
                executable_unpatched_sha256: executableEvidence.unpatchedSha256,
              }
            : {}),
          started_at: startedAt,
          completed_at: new Date().toISOString(),
          error: failure instanceof Error ? failure.message : String(failure),
          desktopPid: child?.pid ?? null,
          backendPid: ready?.backendPid ?? null,
          observedDescendantPids: observedProcesses,
          closeResult: closeResult ?? null,
        },
        null,
        2,
      )}\n`,
    );
    throw failure;
  } finally {
    if (child && !closed) {
      await terminateDesktopTree(child, platform, closePromise);
    }
    if (ready?.appLogDir) copyApplicationLogs(ready.appLogDir, work);
  }
}

async function waitForReadyMarker(control, closePromise, timeoutMs) {
  const readyPath = join(control, "ready.json");
  const failedPath = join(control, "start-failed.json");
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (existsSync(failedPath)) {
      const failure = readJson(failedPath);
      throw new Error(`desktop backend startup failed: ${failure.detail ?? "unknown"}`);
    }
    if (existsSync(readyPath)) return readJson(readyPath);
    const remaining = deadline - Date.now();
    const event = await Promise.race([
      delay(Math.min(POLL_INTERVAL_MS, remaining)).then(() => null),
      closePromise,
    ]);
    if (event) {
      if (event.spawnError) throw new Error(`desktop failed to launch: ${event.spawnError.message}`);
      throw new Error(
        `desktop exited before backend readiness (code=${event.code}, signal=${event.signal ?? "none"})`,
      );
    }
  }
  throw new Error(`desktop did not report backend readiness within ${timeoutMs}ms`);
}

async function waitForJson(path, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (existsSync(path)) return readJson(path);
    await delay(Math.min(POLL_INTERVAL_MS, deadline - Date.now()));
  }
  throw new Error(`timed out waiting for ${path}`);
}

function readJson(path) {
  return JSON.parse(readFileSync(path, "utf8"));
}

async function requireHealthyBackend(origin) {
  const response = await fetch(`${origin}/livez`, { signal: AbortSignal.timeout(3_000) });
  if (!response.ok) throw new Error(`packaged backend /livez returned ${response.status}`);
}

async function requireBackendStopped(origin, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const response = await fetch(`${origin}/livez`, {
        signal: AbortSignal.timeout(500),
      });
      if (!response.ok) return;
    } catch {
      return;
    }
    await delay(POLL_INTERVAL_MS);
  }
  throw new Error(`backend still serves /livez after desktop exit: ${origin}`);
}

function listProcesses(platform) {
  if (platform === "win32") {
    const command = [
      "$items = @(Get-CimInstance Win32_Process | ForEach-Object {",
      "[pscustomobject]@{ pid = [int]$_.ProcessId; ppid = [int]$_.ParentProcessId; state = ''; name = [string]$_.Name }",
      "}); $items | ConvertTo-Json -Compress",
    ].join(" ");
    let result = spawnSync("pwsh", ["-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command], {
      encoding: "utf8",
      windowsHide: true,
    });
    if (result.error?.code === "ENOENT") {
      result = spawnSync(
        "powershell.exe",
        ["-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command],
        { encoding: "utf8", windowsHide: true },
      );
    }
    if (result.status !== 0) {
      throw new Error(`cannot enumerate Windows processes: ${result.stderr || result.error}`);
    }
    const parsed = JSON.parse(result.stdout || "[]");
    return Array.isArray(parsed) ? parsed : [parsed];
  }

  const result = spawnSync("ps", ["-axo", "pid=,ppid=,state=,command="], {
    encoding: "utf8",
  });
  if (result.status !== 0) {
    throw new Error(`cannot enumerate processes: ${result.stderr || result.error}`);
  }
  return parsePosixProcessTable(result.stdout);
}

async function waitForProcessesGone(pids, platform, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  let alive = [];
  while (Date.now() < deadline) {
    const table = listProcesses(platform);
    const running = new Set(
      table.filter((item) => !String(item.state ?? "").startsWith("Z")).map((item) => item.pid),
    );
    alive = pids.filter((pid) => running.has(pid));
    if (alive.length === 0) return;
    await delay(POLL_INTERVAL_MS);
  }
  throw new Error(`desktop left orphan process IDs: ${alive.join(", ")}`);
}

async function terminateDesktopTree(child, platform, closePromise) {
  if (platform === "win32" && child.pid) {
    spawnSync("taskkill", ["/PID", String(child.pid), "/T", "/F"], {
      encoding: "utf8",
      windowsHide: true,
    });
  } else {
    child.kill("SIGTERM");
  }
  try {
    await withTimeout(closePromise, 5_000, "desktop did not terminate during cleanup");
    return;
  } catch {
    child.kill("SIGKILL");
    await withTimeout(closePromise, 5_000, "desktop could not be force-killed");
  }
}

function copyApplicationLogs(appLogDirectory, workDirectory) {
  if (!appLogDirectory || !existsSync(appLogDirectory)) return;
  const source = resolve(appLogDirectory);
  const work = resolve(workDirectory);
  if (source === work || source.startsWith(`${work}/`) || source.startsWith(`${work}\\`)) return;
  const destination = join(work, "application-logs");
  rmSync(destination, { recursive: true, force: true });
  cpSync(source, destination, { recursive: true });
}

async function withTimeout(promise, timeoutMs, message) {
  let timer;
  const timeout = new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Error(message)), timeoutMs);
  });
  try {
    return await Promise.race([promise, timeout]);
  } finally {
    clearTimeout(timer);
  }
}

const LIFECYCLE_USAGE =
  "usage: verify-desktop-lifecycle.mjs --executable <path> --artifact <path> " +
  "--artifact-size <bytes> --artifact-sha256 <digest> --work-dir <path> " +
  "--release-commit <40-char-commit> [--bundle-type deb|rpm]";
const ARTIFACT_VERIFICATION_USAGE =
  "usage: verify-desktop-lifecycle.mjs verify-artifact --report <path> " +
  "--artifact <path> --release-commit <40-char-commit>";

function parseOptions(argv, { allowed, usage }) {
  const values = new Map();
  for (let index = 0; index < argv.length; index += 2) {
    const key = argv[index];
    const value = argv[index + 1];
    if (!key?.startsWith("--") || value === undefined) {
      throw new Error(usage);
    }
    const name = key.slice(2);
    if (!allowed.has(name) || values.has(name)) throw new Error(usage);
    values.set(name, value);
  }
  return values;
}

function parseArguments(argv) {
  const values = parseOptions(argv, {
    allowed: new Set([
      "executable",
      "artifact",
      "artifact-size",
      "artifact-sha256",
      "work-dir",
      "release-commit",
      "bundle-type",
    ]),
    usage: LIFECYCLE_USAGE,
  });
  const executable = values.get("executable");
  const artifact = values.get("artifact");
  const artifactSize = Number(values.get("artifact-size"));
  const artifactSha256 = String(values.get("artifact-sha256") ?? "").toLowerCase();
  const workDirectory = values.get("work-dir");
  const releaseCommit = String(values.get("release-commit") ?? "").toLowerCase();
  if (
    !executable ||
    !artifact ||
    !Number.isSafeInteger(artifactSize) ||
    artifactSize <= 0 ||
    !/^(?!0{64}$)[0-9a-f]{64}$/u.test(artifactSha256) ||
    !workDirectory ||
    !FULL_COMMIT_PATTERN.test(releaseCommit)
  ) {
    throw new Error(LIFECYCLE_USAGE);
  }
  const expectedBundleType = values.get("bundle-type");
  if (
    expectedBundleType !== undefined &&
    !Object.hasOwn(TAURI_LINUX_BUNDLE_MARKERS, expectedBundleType)
  ) {
    throw new Error("--bundle-type must be deb or rpm");
  }
  return {
    executable,
    artifact,
    expectedArtifactSize: artifactSize,
    expectedArtifactSha256: artifactSha256,
    workDirectory,
    sourceCommit: releaseCommit,
    expectedBundleType,
  };
}

function parseArtifactVerificationArguments(argv) {
  const values = parseOptions(argv, {
    allowed: new Set(["report", "artifact", "release-commit"]),
    usage: ARTIFACT_VERIFICATION_USAGE,
  });
  const reportPath = values.get("report");
  const artifact = values.get("artifact");
  const expectedCommit = String(values.get("release-commit") ?? "").toLowerCase();
  if (!reportPath || !artifact || !FULL_COMMIT_PATTERN.test(expectedCommit)) {
    throw new Error(ARTIFACT_VERIFICATION_USAGE);
  }
  return { reportPath, artifact, expectedCommit };
}

async function main() {
  try {
    const argv = process.argv.slice(2);
    if (argv[0] === "verify-artifact") {
      const evidence = verifyLifecycleArtifactBinding(
        parseArtifactVerificationArguments(argv.slice(1)),
      );
      console.log(
        `[verify-desktop-lifecycle] artifact=${evidence.path}, ` +
          `size=${evidence.size}, sha256=${evidence.sha256}, binding=passed`,
      );
    } else {
      await verifyDesktopLifecycle(parseArguments(argv));
    }
  } catch (error) {
    console.error(
      `[verify-desktop-lifecycle] ${error instanceof Error ? error.message : String(error)}`,
    );
    process.exitCode = 1;
  }
}

if (isMainModule(import.meta.url)) {
  await main();
}
