import assert from "node:assert/strict";
import { existsSync, mkdtempSync, readFileSync, rmSync } from "node:fs";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { afterEach, test } from "node:test";
import { spawn } from "node:child_process";
import {
  DEFAULT_STARTUP_TIMEOUT_MS,
  resolveStartupTimeoutMs,
  runBackendSmoke,
} from "./verify-bundle-smoke.mjs";

const fixture = join(
  dirname(fileURLToPath(import.meta.url)),
  "fixtures",
  "verify-bundle-backend-fixture.mjs",
);

const spawnedPids = new Set();
const testRoots = new Set();

afterEach(async () => {
  for (const pid of spawnedPids) {
    if (isRunning(pid)) {
      try {
        process.kill(pid, "SIGKILL");
      } catch {
        // The process may have exited between the probe and kill.
      }
    }
  }
  spawnedPids.clear();
  for (const root of testRoots) {
    rmSync(root, { recursive: true, force: true });
  }
  testRoots.clear();
});

test("startup budget defaults to an absolute 90 seconds", () => {
  assert.equal(DEFAULT_STARTUP_TIMEOUT_MS, 90_000);
  assert.equal(resolveStartupTimeoutMs(undefined), 90_000);
});

test("a positive integer environment override can shorten the startup budget", () => {
  assert.equal(resolveStartupTimeoutMs("75"), 75);
  assert.throws(() => resolveStartupTimeoutMs("0"), /positive integer/i);
  assert.throws(() => resolveStartupTimeoutMs("1.5"), /positive integer/i);
  assert.throws(() => resolveStartupTimeoutMs("nope"), /positive integer/i);
});

test("TERM completion has precise Windows and POSIX expectations", () => {
  assert.deepEqual(expectedTermTermination("win32"), { code: null, signal: "SIGTERM" });
  assert.deepEqual(expectedTermTermination("darwin"), { code: 0, signal: null });
  assert.deepEqual(expectedTermTermination("linux"), { code: 0, signal: null });
});

test("successful startup is terminated, closed, and cleaned up", async () => {
  const run = await runScenario("success", { startupTimeoutMs: 2_000 });

  assert.equal(run.result.ok, true);
  assert.deepEqual(run.result.termination, expectedTermTermination(process.platform));
  assert.equal(existsSync(run.dataDir), false);
  await assertProcessGone(run.pid);
});

test("natural exit before the deadline reports its code instead of a timeout", async () => {
  const run = await runScenario("natural-exit", { startupTimeoutMs: 1_000 });

  assert.equal(run.error.kind, "early-exit");
  assert.equal(run.error.exitCode, 23);
  assert.equal(run.error.exitSignal, null);
  assert.equal(existsSync(run.dataDir), false);
  await assertProcessGone(run.pid);
});

test(
  "natural signal exit before the deadline reports its signal instead of a timeout",
  { skip: process.platform === "win32" },
  async () => {
    const run = await runScenario("natural-signal", { startupTimeoutMs: 1_000 });

    assert.equal(run.error.kind, "early-exit");
    assert.equal(run.error.exitCode, null);
    assert.equal(run.error.exitSignal, "SIGTERM");
    assert.equal(existsSync(run.dataDir), false);
    await assertProcessGone(run.pid);
  },
);

test("an alive backend that misses the deadline is reported as our timeout", async () => {
  const run = await runScenario("timeout", {
    startupTimeoutMs: 80,
    fixtureStartupDelayMs: 150,
  });

  assert.equal(run.error.kind, "timeout");
  assert.match(run.error.message, /80ms/);
  assert.deepEqual(run.error.termination, expectedTermTermination(process.platform));
  assert.equal(existsSync(run.dataDir), false);
  await assertProcessGone(run.pid);
});

test(
  "a backend that ignores TERM is KILLed and closed before cleanup",
  { skip: process.platform === "win32" },
  async () => {
    const run = await runScenario("ignore-term", {
      startupTimeoutMs: 80,
      shutdownGraceMs: 100,
      fixtureStartupDelayMs: 150,
    });

    assert.equal(run.error.kind, "timeout");
    assert.equal(readFileSync(run.termMarker, "utf8"), "received");
    assert.equal(run.error.termination.signal, "SIGKILL");
    assert.equal(existsSync(run.dataDir), false);
    await assertProcessGone(run.pid);
  },
);

async function runScenario(mode, options) {
  const root = mkdtempSync(join(tmpdir(), "verify-bundle-smoke-test-"));
  testRoots.add(root);
  const pidFile = join(root, "pid");
  const termMarker = join(root, "term");
  const port = await reservePort();
  let dataDir;
  const child = spawn(
    process.execPath,
    [
      fixture,
      mode,
      String(port),
      pidFile,
      termMarker,
      String(options.fixtureStartupDelayMs ?? 0),
    ],
    { stdio: ["ignore", "pipe", "pipe", "ipc"] },
  );
  const pid = child.pid;
  if (!pid) throw new Error("fixture did not receive a process ID");
  spawnedPids.add(pid);

  await waitForFixtureReady(child);
  assert.equal(readFileSync(pidFile, "utf8"), String(pid));

  try {
    const result = await runBackendSmoke({
      launch: (createdDataDir) => {
        dataDir = createdDataDir;
        // runBackendSmoke installs exit/close/error listeners synchronously
        // after launch returns, before this fixture is allowed to start.
        setImmediate(() => child.send({ type: "go" }));
        return child;
      },
      url: `http://127.0.0.1:${port}/m`,
      startupTimeoutMs: options.startupTimeoutMs,
      shutdownGraceMs: options.shutdownGraceMs ?? 500,
      killWaitMs: 1_000,
      pollIntervalMs: 10,
      requestTimeoutMs: 50,
    });
    if (!isRunning(pid)) spawnedPids.delete(pid);
    return { result, dataDir, pid, termMarker };
  } catch (error) {
    if (!isRunning(pid)) spawnedPids.delete(pid);
    return { error, dataDir, pid, termMarker };
  }
}

async function waitForFixtureReady(child) {
  await new Promise((resolve, reject) => {
    const timeout = setTimeout(
      () => finish(new Error("fixture did not report ready within 5 seconds")),
      5_000,
    );

    const onMessage = (message) => {
      if (message?.type !== "ready") return;
      if (message.pid !== child.pid) {
        finish(new Error(`fixture reported unexpected process ID ${message.pid}`));
        return;
      }
      finish();
    };
    const onError = (error) => finish(error);
    const onExit = (code, signal) => {
      finish(new Error(`fixture exited before ready (code=${code}, signal=${signal ?? "none"})`));
    };

    child.on("message", onMessage);
    child.once("error", onError);
    child.once("exit", onExit);

    function finish(error) {
      clearTimeout(timeout);
      child.off("message", onMessage);
      child.off("error", onError);
      child.off("exit", onExit);
      if (error) reject(error);
      else resolve();
    }
  });
}

async function reservePort() {
  const server = createServer();
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolve);
  });
  const address = server.address();
  const port = typeof address === "object" && address ? address.port : 0;
  await new Promise((resolve, reject) => {
    server.close((error) => (error ? reject(error) : resolve()));
  });
  return port;
}

function expectedTermTermination(platform) {
  if (platform === "win32") return { code: null, signal: "SIGTERM" };
  return { code: 0, signal: null };
}

function isRunning(pid) {
  try {
    process.kill(pid, 0);
    return true;
  } catch (error) {
    return error?.code !== "ESRCH";
  }
}

async function assertProcessGone(pid) {
  const deadline = Date.now() + 1_000;
  while (Date.now() < deadline) {
    if (!isRunning(pid)) return;
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
  assert.fail(`process ${pid} is still running`);
}
