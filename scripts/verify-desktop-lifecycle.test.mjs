import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import {
  chmodSync,
  mkdirSync,
  mkdtempSync,
  realpathSync,
  rmSync,
  symlinkSync,
  writeFileSync,
} from "node:fs";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import test from "node:test";

import {
  buildIsolatedEnvironment,
  descendantProcessIds,
  hashStableExecutable,
  parsePosixProcessTable,
  resolveDesktopExecutable,
  validateReadyMarker,
  validateDesktopLifecycleReport,
  verifyDesktopLifecycle,
} from "./verify-desktop-lifecycle.mjs";

test("builds an isolated lifecycle environment without inheriting user data roots", (t) => {
  const directory = mkdtempSync(join(tmpdir(), "suxiaoyou-desktop-lifecycle-"));
  t.after(() => rmSync(directory, { recursive: true, force: true }));

  const { environment, control, home } = buildIsolatedEnvironment(directory, "linux", {
    PATH: "/usr/bin",
    HOME: "/real-home",
  });

  assert.equal(environment.HOME, home);
  assert.equal(environment.SUXIAOYOU_DESKTOP_LIFECYCLE_SMOKE_DIR, control);
  assert.ok(resolve(environment.XDG_DATA_HOME).startsWith(resolve(directory)));
  assert.ok(resolve(environment.TMPDIR).startsWith(resolve(directory)));
  assert.equal(environment.GDK_BACKEND, "x11");
});

test("validates a strict loopback readiness marker", () => {
  const marker = validateReadyMarker(
    {
      desktopPid: 4100,
      backendPid: 4101,
      backendUrl: "http://127.0.0.1:43123",
      appDataDir: "/tmp/data",
      appLogDir: "/tmp/logs",
    },
    4100,
  );
  assert.equal(marker.backendUrl, "http://127.0.0.1:43123");

  for (const backendUrl of [
    "https://127.0.0.1:43123",
    "http://localhost:43123",
    "http://127.0.0.1:43123/path",
  ]) {
    assert.throws(
      () => validateReadyMarker({ ...marker, backendUrl }, 4100),
      /loopback origin/,
    );
  }
});

test("collects the complete descendant tree without unrelated processes", () => {
  const processes = parsePosixProcessTable(`
  10 1 S desktop
  11 10 S backend
  12 11 S helper
  13 10 S webview
  20 1 S unrelated
  `);
  assert.deepEqual(descendantProcessIds(processes, 10), [11, 13, 12]);
});

test("rejects lifecycle reports that do not explicitly prove cleanup", () => {
  const report = {
    schema_version: 1,
    status: "ok",
    ok: true,
    platform: "linux",
    source_commit: "a".repeat(40),
    release_ref: "v1.0.0-rc.7",
    executable_path: "/tmp/suyo-desktop",
    executable_size: 4096,
    executable_sha256: "c".repeat(64),
    started_at: "2026-07-14T00:00:00Z",
    completed_at: "2026-07-14T00:00:01Z",
    desktopPid: 4100,
    backendPid: 4101,
    backendUrl: "http://127.0.0.1:43123",
    observedDescendantPids: [4101],
    exit: { code: 0, signal: null },
    checks: {
      backend_ready: true,
      backend_healthy: true,
      graceful_exit: true,
      no_orphan_processes: false,
      backend_stopped: true,
    },
  };
  const result = validateDesktopLifecycleReport(report, {
    expectedPlatform: "linux",
    expectedCommit: "a".repeat(40),
    expectedReleaseRef: "v1.0.0-rc.7",
  });
  assert.equal(result.ok, false);
  assert.ok(result.failures.includes("checks.no_orphan_processes must be true"));
});

test("validates executable paths with evidence-platform semantics", () => {
  const base = {
    schema_version: 1,
    status: "ok",
    ok: true,
    source_commit: "a".repeat(40),
    release_ref: "v1.0.0-rc.7",
    executable_size: 4096,
    executable_sha256: "c".repeat(64),
    started_at: "2026-07-14T00:00:00Z",
    completed_at: "2026-07-14T00:00:01Z",
    desktopPid: 4100,
    backendPid: 4101,
    backendUrl: "http://127.0.0.1:43123",
    observedDescendantPids: [4101],
    exit: { code: 0, signal: null },
    checks: {
      backend_ready: true,
      backend_healthy: true,
      graceful_exit: true,
      no_orphan_processes: true,
      backend_stopped: true,
    },
  };
  const validate = (platform, executablePath) =>
    validateDesktopLifecycleReport(
      { ...base, platform, executable_path: executablePath },
      { expectedPlatform: platform },
    );

  assert.equal(
    validate(
      "win32",
      "D:\\a\\_temp\\suxiaoyou-nsis-install\\suxiaoyou-desktop.exe",
    ).ok,
    true,
  );
  assert.equal(validate("win32", "D:\\a\\COM10\\suxiaoyou-desktop.exe").ok, true);
  assert.equal(validate("linux", "/opt/suyo/suxiaoyou-desktop").ok, true);
  assert.equal(validate("darwin", "/Applications/苏小有.app/Contents/MacOS/苏小有").ok, true);

  for (const path of [
    "suxiaoyou-desktop.exe",
    "D:suxiaoyou-desktop.exe",
    "\\suxiaoyou-desktop.exe",
    "D:\\a\\..\\suxiaoyou-desktop.exe",
    "D:\\a\\.\\suxiaoyou-desktop.exe",
    "D:/a\\suxiaoyou-desktop.exe",
    "D:\\a\\\\suxiaoyou-desktop.exe",
    "D:\\a\\suxiaoyou-desktop.exe.",
    "D:\\a\\suxiaoyou-desktop.exe ",
    "D:\\a\\suxiaoyou-desktop.exe:alternate-stream",
    "\\\\server\\share\\suxiaoyou-desktop.exe",
    "\\\\?\\D:\\a\\suxiaoyou-desktop.exe",
    "\\\\.\\D:\\a\\suxiaoyou-desktop.exe",
  ]) {
    assert.deepEqual(validate("win32", path).failures, [
      "executable_path must be an absolute canonical path",
    ]);
  }
  for (const reservedName of [
    "CON",
    "con.txt",
    "PRN.log",
    "AUX",
    "NUL.exe",
    "CLOCK$",
    "CLOCK$.txt",
    "CONIN$",
    "CONIN$.txt",
    "CONOUT$",
    "CONOUT$.txt",
    ...Array.from({ length: 9 }, (_, index) => `COM${index + 1}.exe`),
    ...Array.from({ length: 9 }, (_, index) => `LPT${index + 1}.exe`),
    "COM¹.exe",
    "COM².exe",
    "COM³.exe",
    "LPT¹.exe",
    "LPT².exe",
    "LPT³.exe",
  ]) {
    assert.deepEqual(validate("win32", `D:\\a\\${reservedName}`).failures, [
      "executable_path must be an absolute canonical path",
    ]);
  }
  for (const path of [
    "opt/suyo/suxiaoyou-desktop",
    "/opt/suyo/../suxiaoyou-desktop",
    "/opt/suyo/./suxiaoyou-desktop",
    "//opt/suyo/suxiaoyou-desktop",
    "/opt//suyo/suxiaoyou-desktop",
    "/opt/suyo/suxiaoyou-desktop/",
    "/opt/suyo\\suxiaoyou-desktop",
  ]) {
    assert.deepEqual(validate("linux", path).failures, [
      "executable_path must be an absolute canonical path",
    ]);
  }
});

test("hashes the canonical executable and rejects empty files", (t) => {
  const directory = mkdtempSync(join(tmpdir(), "suxiaoyou-desktop-hash-"));
  t.after(() => rmSync(directory, { recursive: true, force: true }));
  const executable = join(directory, "desktop");
  writeFileSync(executable, "packaged desktop bytes\n");
  const evidence = hashStableExecutable(executable);
  assert.equal(evidence.path, realpathSync(executable));
  assert.equal(evidence.size, Buffer.byteLength("packaged desktop bytes\n"));
  assert.match(evidence.sha256, /^[0-9a-f]{64}$/u);

  const empty = join(directory, "empty");
  writeFileSync(empty, "");
  assert.throws(() => hashStableExecutable(empty), /non-empty regular file/u);
});

test("restores exactly one expected Tauri Linux bundle marker before hashing", (t) => {
  const directory = mkdtempSync(join(tmpdir(), "suxiaoyou-bundle-marker-"));
  t.after(() => rmSync(directory, { recursive: true, force: true }));
  const deb = join(directory, "desktop-deb");
  const prefix = Buffer.from("desktop-prefix\0", "utf8");
  const suffix = Buffer.from("\0desktop-suffix", "utf8");
  const marker = Buffer.from("__TAURI_BUNDLE_TYPE_VAR_DEB", "ascii");
  const placeholder = Buffer.from("__TAURI_BUNDLE_TYPE_VAR_UNK", "ascii");
  const bytes = Buffer.concat([prefix, marker, suffix]);
  writeFileSync(deb, bytes);

  const evidence = hashStableExecutable(deb, { expectedBundleType: "deb" });
  assert.equal(evidence.tauriBundleType, "deb");
  assert.equal(
    evidence.sha256,
    createHash("sha256").update(bytes).digest("hex"),
  );
  assert.equal(
    evidence.unpatchedSha256,
    createHash("sha256")
      .update(Buffer.concat([prefix, placeholder, suffix]))
      .digest("hex"),
  );

  assert.throws(
    () => hashStableExecutable(deb, { expectedBundleType: "rpm" }),
    /bundle marker does not match rpm/u,
  );
  const duplicate = join(directory, "desktop-duplicate");
  writeFileSync(duplicate, Buffer.concat([bytes, marker]));
  assert.throws(
    () => hashStableExecutable(duplicate, { expectedBundleType: "deb" }),
    /exactly one Tauri bundle marker/u,
  );
  const missing = join(directory, "desktop-missing");
  writeFileSync(missing, "desktop without a bundle marker");
  assert.throws(
    () => hashStableExecutable(missing, { expectedBundleType: "deb" }),
    /exactly one Tauri bundle marker/u,
  );
});

test("validates release-bound Tauri bundle evidence", () => {
  const report = {
    schema_version: 1,
    status: "ok",
    ok: true,
    platform: "linux",
    source_commit: "a".repeat(40),
    release_ref: "v1.0.0-rc.7",
    executable_path: "/usr/bin/suxiaoyou-desktop",
    executable_size: 4096,
    executable_sha256: "c".repeat(64),
    tauri_bundle_type: "deb",
    executable_unpatched_sha256: "d".repeat(64),
    started_at: "2026-07-14T00:00:00Z",
    completed_at: "2026-07-14T00:00:01Z",
    desktopPid: 4100,
    backendPid: 4101,
    backendUrl: "http://127.0.0.1:43123",
    observedDescendantPids: [4101],
    exit: { code: 0, signal: null },
    checks: {
      backend_ready: true,
      backend_healthy: true,
      graceful_exit: true,
      no_orphan_processes: true,
      backend_stopped: true,
    },
  };
  assert.equal(
    validateDesktopLifecycleReport(report, {
      expectedPlatform: "linux",
      expectedBundleType: "deb",
    }).ok,
    true,
  );
  assert.ok(
    validateDesktopLifecycleReport(report, {
      expectedPlatform: "linux",
      expectedBundleType: "rpm",
    }).failures.some((failure) => failure.includes("expected rpm")),
  );
  assert.ok(
    validateDesktopLifecycleReport(
      { ...report, executable_unpatched_sha256: undefined },
      { expectedPlatform: "linux", expectedBundleType: "deb" },
    ).failures.includes(
      "executable_unpatched_sha256 must be a non-zero SHA-256 digest",
    ),
  );
  assert.ok(
    validateDesktopLifecycleReport(
      { ...report, platform: "win32" },
      { expectedPlatform: "win32", expectedBundleType: "deb" },
    ).failures.includes("Tauri bundle evidence is only valid for linux reports"),
  );
});

test(
  "canonicalizes a desktop executable beneath a symlinked ancestor",
  { skip: process.platform === "win32" },
  (t) => {
    const directory = mkdtempSync(join(tmpdir(), "suxiaoyou-desktop-realpath-"));
    t.after(() => rmSync(directory, { recursive: true, force: true }));
    const realDirectory = join(directory, "installed");
    const linkedDirectory = join(directory, "installed-link");
    mkdirSync(realDirectory);
    const executable = join(realDirectory, "desktop");
    writeFileSync(executable, "desktop\n");
    symlinkSync(realDirectory, linkedDirectory, "dir");

    const requested = join(linkedDirectory, "desktop");
    const canonical = resolveDesktopExecutable(requested);

    assert.equal(canonical, realpathSync(executable));
    assert.notEqual(canonical, resolve(requested));
  },
);

test(
  "probes readiness, requests graceful exit, and observes descendant cleanup end to end",
  { skip: process.platform === "win32" },
  async (t) => {
    const directory = mkdtempSync(join(tmpdir(), "suxiaoyou-desktop-lifecycle-e2e-"));
    t.after(() => rmSync(directory, { recursive: true, force: true }));
    const executable = join(directory, "fake-desktop");
    const workDirectory = join(directory, "diagnostics");
    const port = await unusedPort();
    writeFileSync(
      executable,
      `#!/usr/bin/env node
const { spawn } = require("node:child_process");
const { existsSync, mkdirSync, writeFileSync } = require("node:fs");
const { join } = require("node:path");
const control = process.env.SUXIAOYOU_DESKTOP_LIFECYCLE_SMOKE_DIR;
const logDir = join(process.env.HOME, "logs");
const dataDir = join(process.env.HOME, "data");
mkdirSync(control, { recursive: true });
mkdirSync(logDir, { recursive: true });
mkdirSync(dataDir, { recursive: true });
const backend = spawn(process.execPath, ["-e", ${JSON.stringify(`
  const http = require("node:http");
  const server = http.createServer((request, response) => {
    response.statusCode = request.url === "/livez" ? 200 : 404;
    response.end(request.url === "/livez" ? "ok" : "missing");
  });
  server.listen(${port}, "127.0.0.1", () => process.send?.("ready"));
`)}], { stdio: ["ignore", "ignore", "ignore", "ipc"] });
const marker = {
  desktopPid: process.pid,
  backendPid: backend.pid,
  backendUrl: "http://127.0.0.1:${port}",
  appDataDir: dataDir,
  appLogDir: logDir,
};
backend.once("message", () => writeFileSync(join(control, "ready.json"), JSON.stringify(marker)));
const poll = setInterval(() => {
  try {
    const request = join(control, "request-exit");
    if (!existsSync(request)) return;
    clearInterval(poll);
    backend.once("exit", () => {
      writeFileSync(join(control, "cleanup.json"), JSON.stringify({
        desktopPid: process.pid,
        backendCleanupOk: true,
      }));
      process.exit(0);
    });
    backend.kill("SIGTERM");
  } catch (error) {
    console.error(error);
    process.exit(1);
  }
}, 50);
`,
    );
    chmodSync(executable, 0o755);

    const result = await verifyDesktopLifecycle({
      executable,
      workDirectory,
      sourceCommit: "a".repeat(40),
      releaseRef: "v1.0.0-rc.7",
    });
    assert.equal(result.ok, true);
    assert.equal(result.status, "ok");
    assert.equal(result.schema_version, 1);
    assert.equal(result.executable_path, realpathSync(executable));
    assert.ok(result.executable_size > 0);
    assert.match(result.executable_sha256, /^[0-9a-f]{64}$/u);
    assert.equal(result.backendUrl, `http://127.0.0.1:${port}`);
    assert.ok(result.observedDescendantPids.includes(result.backendPid));
    assert.equal(result.checks.no_orphan_processes, true);
    assert.equal(
      validateDesktopLifecycleReport(result, {
        expectedPlatform: process.platform,
        expectedCommit: "a".repeat(40),
        expectedReleaseRef: "v1.0.0-rc.7",
      }).ok,
      true,
    );
  },
);

async function unusedPort() {
  const server = createServer();
  await new Promise((resolvePromise, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolvePromise);
  });
  const address = server.address();
  const port = typeof address === "object" && address ? address.port : 0;
  await new Promise((resolvePromise, reject) =>
    server.close((error) => (error ? reject(error) : resolvePromise())),
  );
  return port;
}
