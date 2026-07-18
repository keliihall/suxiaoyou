import assert from "node:assert/strict";
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import test from "node:test";

import {
  verifyAcpClosedGate,
  verifyAcpProtocolSmoke,
} from "./verify-acp-bundle.mjs";


test("accepts exit 78 with empty protocol stdout for the closed ACP entry", () => {
  const result = verifyAcpClosedGate({
    command: process.execPath,
    args: ["-e", "process.exit(78)"],
  });

  assert.deepEqual(result, { exitCode: 78, stdoutBytes: 0 });
});


test("rejects protocol stdout pollution even when the ACP gate exits 78", () => {
  assert.throws(
    () =>
      verifyAcpClosedGate({
        command: process.execPath,
        args: [
          "-e",
          "process.stdout.write('not-json-rpc'); process.exit(78)",
        ],
      }),
    /wrote to protocol stdout/,
  );
});


test("proves initialize, new session, update, and cancel over process stdio", async (t) => {
  const fixture = createProtocolFixture(t);
  const report = await verifyAcpProtocolSmoke({
    command: process.execPath,
    args: [fixture],
    cwd: dirname(fixture),
  });

  assert.equal(report.protocolVersion, 1);
  assert.equal(report.sessionId, "bundle-smoke-session");
  assert.equal(report.stopReason, "cancelled");
  assert.equal(report.frameCount, 4);
});


test("rejects a prompt that does not fail closed after cancellation", async (t) => {
  const fixture = createProtocolFixture(t);
  await assert.rejects(
    verifyAcpProtocolSmoke({
      command: process.execPath,
      args: [fixture],
      cwd: dirname(fixture),
      environment: { ...process.env, ACP_BAD_STOP: "1" },
    }),
    /did not fail closed on cancel/,
  );
});


function createProtocolFixture(t) {
  const directory = mkdtempSync(join(tmpdir(), "suxiaoyou-acp-smoke-"));
  t.after(() => rmSync(directory, { recursive: true, force: true }));
  const fixture = join(directory, "agent.mjs");
  mkdirSync(directory, { recursive: true });
  writeFileSync(
    fixture,
    `
import { createInterface } from "node:readline";

const lines = createInterface({ input: process.stdin, crlfDelay: Infinity });
let promptId = null;
const sessionId = "bundle-smoke-session";
const send = (message) => process.stdout.write(JSON.stringify({ jsonrpc: "2.0", ...message }) + "\\n");

for await (const line of lines) {
  const message = JSON.parse(line);
  if (message.method === "initialize") {
    send({ id: message.id, result: {
      protocolVersion: 1,
      agentCapabilities: {},
      agentInfo: { name: "suxiaoyou", version: "fixture" },
      authMethods: [],
    } });
  } else if (message.method === "session/new") {
    send({ id: message.id, result: { sessionId } });
  } else if (message.method === "session/prompt") {
    promptId = message.id;
    send({ method: "session/update", params: {
      sessionId,
      update: {
        sessionUpdate: "agent_message_chunk",
        content: { type: "text", text: "bundle-smoke-ready" },
      },
    } });
  } else if (message.method === "session/cancel") {
    send({
      id: promptId,
      result: { stopReason: process.env.ACP_BAD_STOP ? "end_turn" : "cancelled" },
    });
  }
}
`,
    "utf8",
  );
  return fixture;
}
