import assert from "node:assert/strict";
import { mkdirSync, mkdtempSync, rmSync, unlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, test } from "node:test";

import { verifyNodeRuntime } from "./verify-node-runtime.mjs";

const temporaryDirectories = [];

afterEach(() => {
  for (const directory of temporaryDirectories.splice(0)) {
    rmSync(directory, { recursive: true, force: true });
  }
});

function runtimeFixture(platform) {
  const runtime = mkdtempSync(join(tmpdir(), "verify-node-runtime-"));
  temporaryDirectories.push(runtime);
  const isWindows = platform === "win32";
  const binDirectory = isWindows ? runtime : join(runtime, "bin");
  mkdirSync(binDirectory, { recursive: true });
  const paths = {
    node: join(binDirectory, isWindows ? "node.exe" : "node"),
    npm: join(binDirectory, isWindows ? "npm.cmd" : "npm"),
    npx: join(binDirectory, isWindows ? "npx.cmd" : "npx"),
  };
  for (const path of Object.values(paths)) writeFileSync(path, "fixture");
  return { binDirectory, paths, runtime };
}

function commandRunner(fixture, platform, overrides = {}) {
  const calls = [];
  const runCommand = async (command, args, options = {}) => {
    calls.push({ command, args, options });
    if (command === fixture.paths.node && args.join(" ") === "--version") {
      return { stdout: `${overrides.nodeVersion ?? "v22.22.0"}\n` };
    }
    if (command === fixture.paths.node && args.join(" ") === "-p process.arch") {
      return { stdout: `${overrides.nodeArchitecture ?? "arm64"}\n` };
    }
    if (command === fixture.paths.node && args.join(" ") === "-p process.execPath") {
      return { stdout: `${fixture.paths.node}\n` };
    }
    if (platform !== "win32" && command === fixture.paths.npm) {
      return { stdout: `${overrides.npmVersion ?? "10.9.4"}\n` };
    }
    if (platform !== "win32" && command === fixture.paths.npx) {
      return { stdout: `${overrides.npxVersion ?? "10.9.4"}\n` };
    }
    if (platform === "win32" && command === "cmd.exe") {
      const tool = args.at(-1).includes("npm.cmd") ? "npm" : "npx";
      return { stdout: `${overrides[`${tool}Version`] ?? "10.9.4"}\n` };
    }
    throw new Error(`unexpected command: ${command} ${args.join(" ")}`);
  };
  return { calls, runCommand };
}

test("verifies a Unix node, npm and npx runtime with bundled PATH first", async () => {
  const fixture = runtimeFixture("darwin");
  const runner = commandRunner(fixture, "darwin");

  const result = await verifyNodeRuntime(fixture.runtime, {
    platform: "darwin",
    expectedArchitecture: "arm64",
    env: { PATH: "/usr/bin:/bin" },
    runCommand: runner.runCommand,
    log: () => {},
  });

  assert.equal(result.nodeVersion, "v22.22.0");
  assert.equal(result.npmVersion, "10.9.4");
  assert.equal(result.npxVersion, "10.9.4");
  assert.ok(
    runner.calls.every(({ options }) =>
      options.env.PATH.startsWith(`${fixture.binDirectory}:`),
    ),
  );
});

test("uses cmd.exe for Windows npm.cmd and npx.cmd", async () => {
  const fixture = runtimeFixture("win32");
  const runner = commandRunner(fixture, "win32", { nodeArchitecture: "x64" });

  const result = await verifyNodeRuntime(fixture.runtime, {
    platform: "win32",
    expectedArchitecture: "x64",
    env: { PATH: "C:\\Windows\\System32", COMSPEC: "cmd.exe" },
    runCommand: runner.runCommand,
    log: () => {},
  });

  assert.equal(result.nodeArchitecture, "x64");
  const cmdCalls = runner.calls.filter(({ command }) => command === "cmd.exe");
  assert.equal(cmdCalls.length, 2);
  assert.match(cmdCalls[0].args.at(-1), /npm\.cmd" --version/);
  assert.match(cmdCalls[1].args.at(-1), /npx\.cmd" --version/);
  assert.ok(cmdCalls.every(({ options }) => options.env.PATH.startsWith(fixture.runtime)));
});

test("rejects a missing npm entry point", async () => {
  const fixture = runtimeFixture("linux");
  unlinkSync(fixture.paths.npm);

  await assert.rejects(
    verifyNodeRuntime(fixture.runtime, {
      platform: "linux",
      expectedArchitecture: "arm64",
      runCommand: async () => assert.fail("must not execute an incomplete runtime"),
      log: () => {},
    }),
    /bundled npm executable does not exist/,
  );
});

test("rejects the wrong Node version before npm runs", async () => {
  const fixture = runtimeFixture("darwin");
  const runner = commandRunner(fixture, "darwin", { nodeVersion: "v22.21.0" });

  await assert.rejects(
    verifyNodeRuntime(fixture.runtime, {
      platform: "darwin",
      expectedArchitecture: "arm64",
      runCommand: runner.runCommand,
      log: () => {},
    }),
    /expected Node v22\.22\.0, got v22\.21\.0/,
  );
  assert.equal(runner.calls.some(({ command }) => command === fixture.paths.npm), false);
});

test("rejects malformed npm version output", async () => {
  const fixture = runtimeFixture("linux");
  const runner = commandRunner(fixture, "linux", { npmVersion: "broken" });

  await assert.rejects(
    verifyNodeRuntime(fixture.runtime, {
      platform: "linux",
      expectedArchitecture: "arm64",
      runCommand: runner.runCommand,
      log: () => {},
    }),
    /invalid npm version output: broken/,
  );
});
