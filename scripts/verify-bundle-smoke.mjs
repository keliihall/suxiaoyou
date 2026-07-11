import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { setTimeout as delay } from "node:timers/promises";

export const DEFAULT_STARTUP_TIMEOUT_MS = 90_000;

const DEFAULT_SHUTDOWN_GRACE_MS = 3_000;
const DEFAULT_KILL_WAIT_MS = 5_000;
const DEFAULT_POLL_INTERVAL_MS = 500;
const DEFAULT_REQUEST_TIMEOUT_MS = 2_000;

export class BackendSmokeError extends Error {
  constructor(kind, message, details = {}) {
    super(message);
    this.name = "BackendSmokeError";
    this.kind = kind;
    Object.assign(this, details);
  }
}

/**
 * Parse VERIFY_BUNDLE_STARTUP_TIMEOUT_MS. Tests may deliberately use a short
 * positive value, while release builds get a 90-second absolute cold-start
 * budget for older Macs.
 */
export function resolveStartupTimeoutMs(rawValue) {
  if (rawValue === undefined || rawValue === "") return DEFAULT_STARTUP_TIMEOUT_MS;
  if (!/^[1-9]\d*$/.test(rawValue)) {
    throw new Error("VERIFY_BUNDLE_STARTUP_TIMEOUT_MS must be a positive integer in milliseconds");
  }
  const value = Number(rawValue);
  if (!Number.isSafeInteger(value)) {
    throw new Error("VERIFY_BUNDLE_STARTUP_TIMEOUT_MS must be a positive integer in milliseconds");
  }
  return value;
}

/**
 * Launch and probe a real backend process, then always reap it before deleting
 * its temporary data directory. `launch` receives that isolated directory and
 * must return a Node ChildProcess.
 */
export async function runBackendSmoke({
  launch,
  url,
  startupTimeoutMs = DEFAULT_STARTUP_TIMEOUT_MS,
  shutdownGraceMs = DEFAULT_SHUTDOWN_GRACE_MS,
  killWaitMs = DEFAULT_KILL_WAIT_MS,
  pollIntervalMs = DEFAULT_POLL_INTERVAL_MS,
  requestTimeoutMs = DEFAULT_REQUEST_TIMEOUT_MS,
}) {
  validatePositiveInteger("startupTimeoutMs", startupTimeoutMs);
  validatePositiveInteger("shutdownGraceMs", shutdownGraceMs);
  validatePositiveInteger("killWaitMs", killWaitMs);
  validatePositiveInteger("pollIntervalMs", pollIntervalMs);
  validatePositiveInteger("requestTimeoutMs", requestTimeoutMs);
  if (typeof launch !== "function") throw new TypeError("launch must be a function");
  if (typeof url !== "string" || url.length === 0) throw new TypeError("url must be a string");

  const dataDir = mkdtempSync(join(tmpdir(), "suxiaoyou-smoke-"));
  let child;
  let primaryError;
  let ready = false;
  let termination;
  let closed = false;
  const logs = [];

  try {
    child = launch(dataDir);
    if (!child || typeof child.once !== "function" || typeof child.kill !== "function") {
      throw new TypeError("launch must return a ChildProcess");
    }

    child.stdout?.on("data", (chunk) => logs.push(chunk.toString()));
    child.stderr?.on("data", (chunk) => logs.push(chunk.toString()));

    let exitStatus;
    let spawnError;
    let resolveExit;
    let resolveClose;
    const exitPromise = new Promise((resolve) => {
      resolveExit = resolve;
    });
    const closePromise = new Promise((resolve) => {
      resolveClose = resolve;
    });

    child.once("error", (error) => {
      spawnError = error;
      resolveExit();
    });
    child.once("exit", (code, signal) => {
      exitStatus = { code, signal, at: Date.now() };
      resolveExit();
    });
    child.once("close", (code, signal) => {
      closed = true;
      termination = { code, signal };
      resolveClose(termination);
    });

    const deadline = Date.now() + startupTimeoutMs;

    while (true) {
      if (spawnError) {
        primaryError = new BackendSmokeError(
          "spawn-error",
          `backend failed to launch: ${spawnError.message}`,
          { cause: spawnError },
        );
        break;
      }
      if (exitStatus && exitStatus.at <= deadline) {
        primaryError = earlyExitError(exitStatus);
        break;
      }

      const remainingMs = deadline - Date.now();
      if (remainingMs <= 0) {
        primaryError = new BackendSmokeError(
          "timeout",
          `backend did not serve /m within ${startupTimeoutMs}ms`,
        );
        break;
      }

      try {
        const response = await fetch(url, {
          signal: AbortSignal.timeout(Math.max(1, Math.min(requestTimeoutMs, remainingMs))),
        });
        if (response.status === 200) {
          const body = await response.text();
          if (body.includes("<html") || body.includes("<!DOCTYPE")) {
            ready = true;
            break;
          }
        }
      } catch {
        // The process is still within its absolute startup budget.
      }

      if (spawnError) continue;
      if (exitStatus && exitStatus.at <= deadline) continue;

      const waitMs = Math.max(1, Math.min(pollIntervalMs, deadline - Date.now()));
      await Promise.race([delay(waitMs), exitPromise]);
    }

    termination = await terminateAndWaitForClose(child, {
      exitAlreadyObserved: Boolean(exitStatus),
      closeAlreadyObserved: () => closed,
      closePromise,
      shutdownGraceMs,
      killWaitMs,
    });
  } catch (error) {
    primaryError = primaryError ?? error;

    if (child && !closed) {
      try {
        // This also handles launch/spawn failures that reached us before the
        // normal teardown path.
        termination = await terminateUntrackedChild(child, {
          closeAlreadyObserved: () => closed,
          shutdownGraceMs,
          killWaitMs,
        });
      } catch (cleanupError) {
        primaryError.cleanupError = cleanupError;
      }
    }
  }

  if (!child || closed) {
    rmSync(dataDir, { recursive: true, force: true });
  } else {
    const cleanupError = new BackendSmokeError(
      "cleanup",
      `backend did not emit close; refusing to remove its live data directory: ${dataDir}`,
    );
    if (primaryError) primaryError.cleanupError = cleanupError;
    else primaryError = cleanupError;
  }

  if (primaryError) {
    primaryError.termination = termination;
    primaryError.logs = logs.join("");
    throw primaryError;
  }

  if (!ready) {
    throw new BackendSmokeError("timeout", `backend did not serve /m within ${startupTimeoutMs}ms`, {
      termination,
      logs: logs.join(""),
    });
  }

  return { ok: true, termination };
}

function earlyExitError({ code, signal }) {
  const codeText = code === null ? "null" : String(code);
  const signalText = signal ?? "none";
  return new BackendSmokeError(
    "early-exit",
    `backend exited before serving /m (code=${codeText}, signal=${signalText})`,
    { exitCode: code, exitSignal: signal },
  );
}

async function terminateAndWaitForClose(
  child,
  {
    exitAlreadyObserved,
    closeAlreadyObserved,
    closePromise,
    shutdownGraceMs,
    killWaitMs,
  },
) {
  if (closeAlreadyObserved()) return closePromise;

  if (exitAlreadyObserved) {
    return waitForClose(closePromise, killWaitMs, "after backend exit");
  }

  child.kill("SIGTERM");
  const graceful = await waitForCloseOrTimeout(closePromise, shutdownGraceMs);
  if (graceful.closed) return graceful.status;

  child.kill("SIGKILL");
  return waitForClose(closePromise, killWaitMs, "after SIGKILL");
}

async function terminateUntrackedChild(
  child,
  { closeAlreadyObserved, shutdownGraceMs, killWaitMs },
) {
  if (closeAlreadyObserved()) return undefined;
  const closePromise = new Promise((resolve) => {
    child.once("close", (code, signal) => resolve({ code, signal }));
  });
  child.kill("SIGTERM");
  const graceful = await waitForCloseOrTimeout(closePromise, shutdownGraceMs);
  if (graceful.closed) return graceful.status;
  child.kill("SIGKILL");
  return waitForClose(closePromise, killWaitMs, "after SIGKILL");
}

async function waitForCloseOrTimeout(closePromise, timeoutMs) {
  return Promise.race([
    closePromise.then((status) => ({ closed: true, status })),
    delay(timeoutMs).then(() => ({ closed: false })),
  ]);
}

async function waitForClose(closePromise, timeoutMs, context) {
  const result = await waitForCloseOrTimeout(closePromise, timeoutMs);
  if (!result.closed) {
    throw new BackendSmokeError("cleanup", `backend did not emit close ${context}`);
  }
  return result.status;
}

function validatePositiveInteger(name, value) {
  if (!Number.isSafeInteger(value) || value <= 0) {
    throw new TypeError(`${name} must be a positive integer`);
  }
}
