import { spawn, spawnSync } from "node:child_process";

const CLOSED_GATE_EXIT = 78;
const MAX_STDIO_CAPTURE = 1024 * 1024;

/**
 * Prove the production ACP entry fails before consuming stdin while its
 * code-owned release gate is closed.
 */
export function verifyAcpClosedGate({
  command,
  args = [],
  cwd,
  environment = process.env,
  timeoutMs = 30_000,
}) {
  const result = spawnSync(command, args, {
    cwd,
    env: environment,
    encoding: "utf8",
    input:
      '{"jsonrpc":"2.0","id":"must-not-be-consumed","method":"initialize",' +
      '"params":{"protocolVersion":1,"clientCapabilities":{}}}\n',
    maxBuffer: MAX_STDIO_CAPTURE,
    timeout: timeoutMs,
    windowsHide: true,
  });
  if (result.error) {
    throw new Error(`ACP closed-gate entry failed to launch: ${result.error.message}`);
  }
  if (result.status !== CLOSED_GATE_EXIT) {
    throw new Error(
      `ACP closed-gate entry exited ${result.status ?? result.signal}; expected ${CLOSED_GATE_EXIT}: ` +
        tail(result.stderr || result.stdout),
    );
  }
  if (String(result.stdout || "") !== "") {
    throw new Error("ACP closed-gate entry wrote to protocol stdout");
  }
  return { exitCode: result.status, stdoutBytes: 0 };
}

/**
 * Exercise the frozen SDK and its real process pipes through a synthetic,
 * authority-free bridge: initialize -> session/new -> prompt -> cancel.
 */
export async function verifyAcpProtocolSmoke({
  command,
  args = [],
  cwd,
  environment = process.env,
  timeoutMs = 30_000,
}) {
  const deadline = Date.now() + timeoutMs;
  const child = spawn(command, args, {
    cwd,
    env: environment,
    stdio: ["pipe", "pipe", "pipe"],
    windowsHide: true,
  });
  let stdoutBuffer = "";
  let stderr = "";
  let closed = null;
  let protocolError = null;
  const messages = [];
  const waiters = new Set();

  const closedPromise = new Promise((resolve) => {
    child.once("close", (code, signal) => {
      closed = { code, signal };
      const error = new Error(
        `ACP protocol process exited ${code ?? signal}: ${tail(stderr)}`,
      );
      for (const waiter of waiters) waiter.reject(error);
      waiters.clear();
      resolve(closed);
    });
  });

  child.once("error", (error) => {
    protocolError = new Error(`ACP protocol process failed to launch: ${error.message}`);
    for (const waiter of waiters) waiter.reject(protocolError);
    waiters.clear();
  });
  child.stdin.on("error", (error) => {
    if (closed === null && protocolError === null) {
      protocolError = new Error(`ACP protocol stdin failed: ${error.message}`);
      for (const waiter of waiters) waiter.reject(protocolError);
      waiters.clear();
    }
  });
  child.stderr.on("data", (chunk) => {
    stderr = tail(stderr + String(chunk));
  });
  child.stdout.setEncoding("utf8");
  child.stdout.on("data", (chunk) => {
    if (protocolError !== null) return;
    stdoutBuffer += chunk;
    if (stdoutBuffer.length > MAX_STDIO_CAPTURE) {
      protocolError = new Error("ACP protocol stdout exceeded its smoke-test bound");
      for (const waiter of waiters) waiter.reject(protocolError);
      waiters.clear();
      return;
    }
    while (true) {
      const newline = stdoutBuffer.indexOf("\n");
      if (newline < 0) break;
      const line = stdoutBuffer.slice(0, newline);
      stdoutBuffer = stdoutBuffer.slice(newline + 1);
      if (!line) {
        protocolError = new Error("ACP protocol emitted a blank stdout frame");
        break;
      }
      let message;
      try {
        message = JSON.parse(line);
      } catch {
        protocolError = new Error("ACP protocol emitted non-JSON stdout");
        break;
      }
      if (!message || typeof message !== "object" || message.jsonrpc !== "2.0") {
        protocolError = new Error("ACP protocol emitted an invalid JSON-RPC frame");
        break;
      }
      messages.push(message);
      for (const waiter of [...waiters]) {
        if (!waiter.predicate(message)) continue;
        clearTimeout(waiter.timer);
        waiters.delete(waiter);
        waiter.resolve(message);
      }
    }
    if (protocolError !== null) {
      for (const waiter of waiters) waiter.reject(protocolError);
      waiters.clear();
    }
  });

  function remainingMs(label) {
    const remaining = deadline - Date.now();
    if (remaining <= 0) throw new Error(`ACP protocol timed out waiting for ${label}`);
    return remaining;
  }

  function waitForMessage(predicate, label) {
    if (protocolError !== null) return Promise.reject(protocolError);
    const existing = messages.find(predicate);
    if (existing) return Promise.resolve(existing);
    if (closed !== null) {
      return Promise.reject(
        new Error(`ACP protocol process exited before ${label}: ${tail(stderr)}`),
      );
    }
    return new Promise((resolve, reject) => {
      const waiter = { predicate, resolve, reject, timer: null };
      waiter.timer = setTimeout(() => {
        waiters.delete(waiter);
        reject(new Error(`ACP protocol timed out waiting for ${label}`));
      }, remainingMs(label));
      waiters.add(waiter);
    });
  }

  function send(message) {
    if (protocolError !== null) throw protocolError;
    if (closed !== null || child.stdin.destroyed) {
      throw new Error(`ACP protocol process exited before input: ${tail(stderr)}`);
    }
    child.stdin.write(`${JSON.stringify(message)}\n`);
  }

  try {
    send({
      jsonrpc: "2.0",
      id: "bundle-initialize",
      method: "initialize",
      params: {
        protocolVersion: 1,
        clientCapabilities: {},
        clientInfo: { name: "suxiaoyou-bundle-verifier", version: "1" },
      },
    });
    const initialized = await waitForMessage(
      (message) => message.id === "bundle-initialize" && !("method" in message),
      "initialize response",
    );
    if (
      initialized.result?.protocolVersion !== 1 ||
      initialized.result?.agentInfo?.name !== "suxiaoyou" ||
      !Array.isArray(initialized.result?.authMethods)
    ) {
      throw new Error(`ACP initialize response was invalid: ${JSON.stringify(initialized)}`);
    }

    send({
      jsonrpc: "2.0",
      id: "bundle-new",
      method: "session/new",
      params: { cwd, mcpServers: [] },
    });
    const created = await waitForMessage(
      (message) => message.id === "bundle-new" && !("method" in message),
      "session/new response",
    );
    const sessionId = created.result?.sessionId;
    if (sessionId !== "bundle-smoke-session") {
      throw new Error(`ACP session/new response was invalid: ${JSON.stringify(created)}`);
    }

    send({
      jsonrpc: "2.0",
      id: "bundle-prompt",
      method: "session/prompt",
      params: {
        sessionId,
        prompt: [{ type: "text", text: "wait for cancellation" }],
      },
    });
    await waitForMessage(
      (message) =>
        message.method === "session/update" &&
        message.params?.sessionId === sessionId &&
        message.params?.update?.sessionUpdate === "agent_message_chunk" &&
        message.params?.update?.content?.text === "bundle-smoke-ready",
      "session/update notification",
    );
    send({
      jsonrpc: "2.0",
      method: "session/cancel",
      params: { sessionId },
    });
    const prompted = await waitForMessage(
      (message) => message.id === "bundle-prompt" && !("method" in message),
      "cancelled prompt response",
    );
    if (prompted.result?.stopReason !== "cancelled") {
      throw new Error(`ACP prompt did not fail closed on cancel: ${JSON.stringify(prompted)}`);
    }

    child.stdin.end();
    let exitTimer;
    const result = await Promise.race([
      closedPromise,
      new Promise((_, reject) => {
        exitTimer = setTimeout(
          () => reject(new Error("ACP protocol process did not exit after stdin EOF")),
          remainingMs("process exit"),
        );
      }),
    ]).finally(() => clearTimeout(exitTimer));
    if (result.code !== 0) {
      throw new Error(`ACP protocol process exited ${result.code ?? result.signal}: ${tail(stderr)}`);
    }
    if (stdoutBuffer !== "") {
      throw new Error("ACP protocol process left an incomplete stdout frame");
    }
    return {
      protocolVersion: initialized.result.protocolVersion,
      sessionId,
      stopReason: prompted.result.stopReason,
      frameCount: messages.length,
    };
  } catch (error) {
    child.stdin.destroy();
    if (closed === null) child.kill();
    let cleanupTimer;
    await Promise.race([
      closedPromise,
      new Promise((resolve) => {
        cleanupTimer = setTimeout(resolve, 2_000);
      }),
    ]).finally(() => clearTimeout(cleanupTimer));
    throw error;
  }
}

function tail(value) {
  return String(value || "").slice(-4000);
}
