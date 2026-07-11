#!/usr/bin/env node

import { spawn } from "node:child_process";
import { existsSync, readFileSync, realpathSync } from "node:fs";
import { basename, dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const scriptDirectory = dirname(fileURLToPath(import.meta.url));
const repositoryRoot = resolve(scriptDirectory, "..");
const downloaderSource = readFileSync(
  join(repositoryRoot, "backend", "scripts", "download_node.py"),
  "utf8",
);
const nodeVersionMatch = /^NODE_VERSION\s*=\s*["']([^"']+)["']/m.exec(
  downloaderSource,
);
if (!nodeVersionMatch) {
  throw new Error("cannot read NODE_VERSION from backend/scripts/download_node.py");
}

export const EXPECTED_NODE_VERSION = `v${nodeVersionMatch[1]}`;

const SUPPORTED_PLATFORMS = new Set(["win32", "darwin", "linux"]);
const SEMVER_OUTPUT = /^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$/;

export async function verifyNodeRuntime(
  runtimePath,
  {
    platform = process.platform,
    expectedArchitecture = process.arch,
    env = process.env,
    runCommand = defaultRunCommand,
    log = console.log,
  } = {},
) {
  if (!SUPPORTED_PLATFORMS.has(platform)) {
    throw new Error(`unsupported Node runtime platform: ${platform}`);
  }

  const runtime = resolve(runtimePath);
  const isWindows = platform === "win32";
  const binDirectory = isWindows ? runtime : join(runtime, "bin");
  const node = join(binDirectory, isWindows ? "node.exe" : "node");
  const npm = join(binDirectory, isWindows ? "npm.cmd" : "npm");
  const npx = join(binDirectory, isWindows ? "npx.cmd" : "npx");
  const npmCli = isWindows
    ? join(runtime, "node_modules", "npm", "bin", "npm-cli.js")
    : null;
  const npxCli = isWindows
    ? join(runtime, "node_modules", "npm", "bin", "npx-cli.js")
    : null;

  for (const [name, executable] of Object.entries({ node, npm, npx })) {
    if (!existsSync(executable)) {
      throw new Error(`bundled ${name} executable does not exist: ${executable}`);
    }
  }
  if (isWindows) {
    for (const [name, cli] of Object.entries({ npm: npmCli, npx: npxCli })) {
      if (!existsSync(cli)) {
        throw new Error(`bundled ${name} CLI does not exist: ${cli}`);
      }
    }
  }

  const delimiter = isWindows ? ";" : ":";
  const runtimeEnv = {
    ...env,
    PATH: [binDirectory, env.PATH].filter(Boolean).join(delimiter),
  };

  const nodeVersion = await commandText(runCommand, node, ["--version"], {
    env: runtimeEnv,
  });
  if (nodeVersion !== EXPECTED_NODE_VERSION) {
    throw new Error(`expected Node ${EXPECTED_NODE_VERSION}, got ${nodeVersion}`);
  }

  const nodeArchitecture = await commandText(
    runCommand,
    node,
    ["-p", "process.arch"],
    { env: runtimeEnv },
  );
  if (nodeArchitecture !== expectedArchitecture) {
    throw new Error(
      `expected Node process.arch ${expectedArchitecture}, got ${nodeArchitecture}`,
    );
  }

  const processExecutable = await commandText(
    runCommand,
    node,
    ["-p", "process.execPath"],
    { env: runtimeEnv },
  );
  const expectedExecutable = normalizeExecutable(node, isWindows);
  const actualExecutable = normalizeExecutable(processExecutable, isWindows);
  if (actualExecutable !== expectedExecutable) {
    throw new Error(
      `bundled node resolved to ${processExecutable}, expected ${node}`,
    );
  }

  // On Windows, run the official npm/npx JavaScript entry points with the
  // bundled node.exe.  This validates the tools without layering Python or
  // Node argv escaping on top of cmd.exe's own quote parser.  The .cmd files
  // remain mandatory packaged launchers above.
  const npmVersion = await commandText(
    runCommand,
    isWindows ? node : npm,
    isWindows ? [npmCli, "--version"] : ["--version"],
    { env: runtimeEnv },
  );
  const npxVersion = await commandText(
    runCommand,
    isWindows ? node : npx,
    isWindows ? [npxCli, "--version"] : ["--version"],
    { env: runtimeEnv },
  );
  for (const [name, version] of Object.entries({ npm: npmVersion, npx: npxVersion })) {
    if (!SEMVER_OUTPUT.test(version)) {
      throw new Error(`invalid ${name} version output: ${version || "no output"}`);
    }
  }

  log(
    `[verify-node-runtime] Node=${nodeVersion}/${nodeArchitecture}, ` +
      `npm=${npmVersion}, npx=${npxVersion}, runtime=${runtime}`,
  );
  return {
    binDirectory,
    node,
    nodeArchitecture,
    nodeVersion,
    npm,
    npmVersion,
    npx,
    npxVersion,
    runtime,
  };
}

function normalizeExecutable(path, isWindows) {
  let normalized;
  try {
    normalized = realpathSync(path);
  } catch {
    normalized = resolve(path);
  }
  return isWindows ? normalized.toLowerCase() : normalized;
}

async function commandText(runCommand, command, args, options) {
  const result = await runCommand(command, args, options);
  return String(result?.stdout ?? result ?? "").trim();
}

function defaultRunCommand(command, args, options = {}) {
  return new Promise((resolveCommand, rejectCommand) => {
    const child = spawn(command, args, {
      env: options.env ?? process.env,
      stdio: ["ignore", "pipe", "pipe"],
    });
    const stdout = [];
    const stderr = [];
    child.stdout.on("data", (chunk) => stdout.push(chunk));
    child.stderr.on("data", (chunk) => stderr.push(chunk));
    child.on("error", rejectCommand);
    child.on("close", (code, signal) => {
      const result = {
        stdout: Buffer.concat(stdout).toString("utf8"),
        stderr: Buffer.concat(stderr).toString("utf8"),
      };
      if (code === 0) {
        resolveCommand(result);
        return;
      }
      rejectCommand(
        new Error(
          `${basename(command)} ${args.join(" ")} failed with ` +
            `${signal ? `signal ${signal}` : `exit ${code}`}\n` +
            `${result.stderr || result.stdout}`,
        ),
      );
    });
  });
}

function isMainModule() {
  return Boolean(process.argv[1]) &&
    realpathSync(process.argv[1]) === realpathSync(fileURLToPath(import.meta.url));
}

if (isMainModule()) {
  const runtimePath = process.argv[2];
  if (!runtimePath || process.argv.length !== 3) {
    console.error("Usage: node scripts/verify-node-runtime.mjs <node-runtime-directory>");
    process.exitCode = 2;
  } else {
    verifyNodeRuntime(runtimePath).catch((error) => {
      console.error(`[verify-node-runtime] ${error.message}`);
      process.exitCode = 1;
    });
  }
}
