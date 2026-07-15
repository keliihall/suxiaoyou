import assert from "node:assert/strict";
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, test } from "node:test";

import {
  declaredMacOSMinimumSystemVersion,
  extractDeploymentTargets,
  verifyMacOSCompatibility,
} from "./verify-macos-compatibility.mjs";

const temporaryDirectories = [];

afterEach(() => {
  for (const directory of temporaryDirectories.splice(0)) {
    rmSync(directory, { recursive: true, force: true });
  }
});

function fixture() {
  const root = mkdtempSync(join(tmpdir(), "macos-compatibility-"));
  temporaryDirectories.push(root);
  const nested = join(root, "_internal");
  mkdirSync(nested);
  const executable = join(root, "suxiaoyou-backend");
  const library = join(nested, "libcrypto.3.dylib");
  const text = join(nested, "metadata.txt");
  for (const path of [executable, library, text]) writeFileSync(path, "fixture");
  return { executable, library, root, text };
}

function commandRunner(fixturePaths, targets = {}) {
  const calls = [];
  const machO = new Set([fixturePaths.executable, fixturePaths.library]);
  const runCommand = async (command, args) => {
    calls.push({ command, args });
    const path = args.at(-1);
    if (command === "file") {
      return {
        stdout: machO.has(path)
          ? "Mach-O 64-bit dynamically linked shared library arm64\n"
          : "ASCII text\n",
      };
    }
    if (command === "vtool") {
      const target = targets[path] ?? "11.0";
      return {
        stdout:
          "Load command 9\n" +
          "      cmd LC_BUILD_VERSION\n" +
          " platform MACOS\n" +
          `    minos ${target}\n` +
          "     tool LD\n",
      };
    }
    throw new Error(`unexpected command: ${command}`);
  };
  return { calls, runCommand };
}

test("reads the compatibility promise from the shipping Tauri config", () => {
  assert.equal(declaredMacOSMinimumSystemVersion(), "11.0");
});

test("checks every Mach-O and accepts targets no newer than the declaration", async () => {
  const paths = fixture();
  const runner = commandRunner(paths, {
    [paths.executable]: "10.15",
    [paths.library]: "11.0",
  });

  const result = await verifyMacOSCompatibility(paths.root, "11.0", {
    platform: "darwin",
    runCommand: runner.runCommand,
    log: () => {},
  });

  assert.equal(result.skipped, false);
  assert.equal(result.machOCount, 2);
  assert.equal(
    runner.calls.filter(({ command }) => command === "file").length,
    3,
  );
  assert.equal(
    runner.calls.filter(({ command }) => command === "vtool").length,
    2,
  );
});

test("rejects a transitive Homebrew library targeting a newer macOS", async () => {
  const paths = fixture();
  const runner = commandRunner(paths, { [paths.library]: "26.0" });

  await assert.rejects(
    verifyMacOSCompatibility(paths.root, "11.0", {
      platform: "darwin",
      runCommand: runner.runCommand,
      log: () => {},
    }),
    /libcrypto\.3\.dylib.*deployment target 26\.0 exceeds.*11\.0/,
  );
});

test("fails closed when vtool cannot establish a deployment target", async () => {
  const paths = fixture();
  const runner = commandRunner(paths);
  const runCommand = async (command, args) => {
    if (command === "vtool") return { stdout: "no load commands\n" };
    return runner.runCommand(command, args);
  };

  await assert.rejects(
    verifyMacOSCompatibility(paths.root, "11.0", {
      platform: "darwin",
      runCommand,
      log: () => {},
    }),
    /vtool reported no macOS deployment target; refusing to build/,
  );
});

test("non-macOS builds require the output path but skip Mach-O commands", async () => {
  const paths = fixture();
  let commandCalled = false;

  const result = await verifyMacOSCompatibility(paths.root, "11.0", {
    platform: "linux",
    runCommand: async () => {
      commandCalled = true;
      throw new Error("must not run");
    },
    log: () => {},
  });

  assert.equal(result.skipped, true);
  assert.equal(commandCalled, false);
  await assert.rejects(
    verifyMacOSCompatibility(join(paths.root, "missing"), "11.0", {
      platform: "win32",
      log: () => {},
    }),
    /input does not exist/,
  );
});

test("parses both modern and legacy macOS deployment load commands", () => {
  assert.deepEqual(
    extractDeploymentTargets(
      "cmd LC_BUILD_VERSION\nplatform MACOS\nminos 11.0\n" +
        "cmd LC_VERSION_MIN_MACOSX\nversion 10.15\n",
    ),
    ["11.0", "10.15"],
  );
});
