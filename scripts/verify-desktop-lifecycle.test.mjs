import assert from "node:assert/strict";
import { chmodSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import test from "node:test";

import {
  buildIsolatedEnvironment,
  descendantProcessIds,
  parsePosixProcessTable,
  validateReadyMarker,
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

    const result = await verifyDesktopLifecycle({ executable, workDirectory });
    assert.equal(result.ok, true);
    assert.equal(result.backendUrl, `http://127.0.0.1:${port}`);
    assert.ok(result.observedDescendantPids.includes(result.backendPid));
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
