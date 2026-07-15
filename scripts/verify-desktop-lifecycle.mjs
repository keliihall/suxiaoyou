#!/usr/bin/env node

/**
 * Launch a packaged 苏小有 desktop executable, wait for the native shell to
 * report that its embedded backend is ready, request the real graceful Quit
 * path, and prove the observed child process tree was reaped.
 *
 * The executable exposes the control files only when release CI supplies
 * SUXIAOYOU_DESKTOP_LIFECYCLE_SMOKE_DIR. No authentication token is read or
 * written by this verifier.
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
import { dirname, isAbsolute, join, resolve } from "node:path";
import { setTimeout as delay } from "node:timers/promises";

import { isMainModule } from "./release-metadata.mjs";
import { resolveCheckoutCommit } from "./office-contract-evidence.mjs";

const DEFAULT_STARTUP_TIMEOUT_MS = 120_000;
const DEFAULT_SHUTDOWN_TIMEOUT_MS = 45_000;
const POLL_INTERVAL_MS = 200;

export const DESKTOP_LIFECYCLE_SCHEMA_VERSION = 1;

export function validateDesktopLifecycleReport(
  value,
  { expectedPlatform, expectedCommit, expectedReleaseRef } = {},
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
  if (
    typeof report.executable_path !== "string" ||
    !isAbsolute(report.executable_path)
  ) {
    failures.push("executable_path must be an absolute canonical path");
  }
  if (!Number.isSafeInteger(report.executable_size) || report.executable_size <= 0) {
    failures.push("executable_size must be a positive safe integer");
  }
  if (!/^(?!0{64}$)[0-9a-f]{64}$/u.test(String(report.executable_sha256 ?? ""))) {
    failures.push("executable_sha256 must be a non-zero SHA-256 digest");
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

export function hashStableExecutable(executable) {
  const path = resolveDesktopExecutable(executable);
  const before = statSync(path, { bigint: false });
  let descriptor = -1;
  try {
    descriptor = openSync(path, constants.O_RDONLY | (constants.O_NOFOLLOW ?? 0));
    const openedBefore = fstatSync(descriptor, { bigint: false });
    if (!openedBefore.isFile() || openedBefore.size <= 0) {
      throw new Error(`desktop executable must be a non-empty regular file: ${path}`);
    }
    const hash = createHash("sha256");
    const buffer = Buffer.allocUnsafe(1024 * 1024);
    let total = 0;
    while (true) {
      const count = readSync(descriptor, buffer, 0, buffer.length, null);
      if (count === 0) break;
      hash.update(buffer.subarray(0, count));
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
        throw new Error(`desktop executable changed while it was hashed: ${path}`);
      }
    }
    if (total !== before.size) {
      throw new Error(`desktop executable changed while it was hashed: ${path}`);
    }
    return {
      path,
      size: total,
      sha256: hash.digest("hex"),
    };
  } finally {
    if (descriptor >= 0) closeSync(descriptor);
  }
}

export async function verifyDesktopLifecycle({
  executable,
  workDirectory,
  platform = process.platform,
  startupTimeoutMs = DEFAULT_STARTUP_TIMEOUT_MS,
  shutdownTimeoutMs = DEFAULT_SHUTDOWN_TIMEOUT_MS,
  sourceCommit = resolveCheckoutCommit(),
  releaseRef = process.env.GITHUB_REF_NAME || "local",
}) {
  const startedAt = new Date().toISOString();
  const application = resolveDesktopExecutable(executable);
  const executableEvidence = hashStableExecutable(application);
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

    const result = {
      schema_version: DESKTOP_LIFECYCLE_SCHEMA_VERSION,
      status: "ok",
      ok: true,
      platform,
      source_commit: sourceCommit,
      release_ref: releaseRef,
      executable_path: executableEvidence.path,
      executable_size: executableEvidence.size,
      executable_sha256: executableEvidence.sha256,
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
      },
    };
    const validation = validateDesktopLifecycleReport(result, {
      expectedPlatform: platform,
      expectedCommit: sourceCommit,
      expectedReleaseRef: releaseRef,
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
    writeFileSync(
      join(work, "failure.json"),
      `${JSON.stringify(
        {
          schema_version: DESKTOP_LIFECYCLE_SCHEMA_VERSION,
          status: "failed",
          ok: false,
          platform,
          source_commit: sourceCommit,
          release_ref: releaseRef,
          executable_path: executableEvidence.path,
          executable_size: executableEvidence.size,
          executable_sha256: executableEvidence.sha256,
          started_at: startedAt,
          completed_at: new Date().toISOString(),
          error: error instanceof Error ? error.message : String(error),
          desktopPid: child?.pid ?? null,
          backendPid: ready?.backendPid ?? null,
          observedDescendantPids: observedProcesses,
          closeResult: closeResult ?? null,
        },
        null,
        2,
      )}\n`,
    );
    throw error;
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

function parseArguments(argv) {
  const values = new Map();
  for (let index = 0; index < argv.length; index += 2) {
    const key = argv[index];
    const value = argv[index + 1];
    if (!key?.startsWith("--") || value === undefined) {
      throw new Error(
        "usage: verify-desktop-lifecycle.mjs --executable <path> --work-dir <path>",
      );
    }
    values.set(key.slice(2), value);
  }
  const executable = values.get("executable");
  const workDirectory = values.get("work-dir");
  if (!executable || !workDirectory) {
    throw new Error(
      "usage: verify-desktop-lifecycle.mjs --executable <path> --work-dir <path>",
    );
  }
  return { executable, workDirectory };
}

async function main() {
  try {
    await verifyDesktopLifecycle(parseArguments(process.argv.slice(2)));
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
