#!/usr/bin/env node

import { spawn } from "node:child_process";
import { readFileSync, realpathSync } from "node:fs";
import { lstat, readdir } from "node:fs/promises";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const scriptDirectory = dirname(fileURLToPath(import.meta.url));
const defaultRepositoryRoot = resolve(scriptDirectory, "..");

export function declaredMacOSMinimumSystemVersion(
  repositoryRoot = defaultRepositoryRoot,
) {
  const configPath = join(
    repositoryRoot,
    "desktop-tauri",
    "src-tauri",
    "tauri.conf.json",
  );
  const config = JSON.parse(readFileSync(configPath, "utf8"));
  const value = config?.bundle?.macOS?.minimumSystemVersion;
  parseVersion(value, "declared macOS minimum system version");
  return value;
}

/**
 * Fail closed when a macOS build input/output contains a Mach-O whose own
 * deployment target is newer than the app's declared minimum system version.
 * Other platforms still require the path to exist but skip Mach-O inspection.
 */
export async function verifyMacOSCompatibility(
  inputPath,
  expectedMinimumSystemVersion,
  {
    platform = process.platform,
    runCommand = defaultRunCommand,
    log = console.log,
  } = {},
) {
  parseVersion(expectedMinimumSystemVersion, "expected macOS minimum system version");
  const input = resolve(inputPath);
  const files = await collectFiles(input);

  if (platform !== "darwin") {
    log(
      `[verify-macos-compatibility] ${platform}: Mach-O check not applicable; ` +
        `input exists at ${input}`,
    );
    return { input, machOCount: 0, skipped: true };
  }

  const machOFiles = [];
  for (const path of files) {
    const description = await commandText(runCommand, "file", ["-b", path]);
    if (!description.includes("Mach-O")) continue;

    const buildMetadata = await commandText(runCommand, "vtool", [
      "-show-build",
      path,
    ]);
    const deploymentTargets = extractDeploymentTargets(buildMetadata);
    if (deploymentTargets.length === 0) {
      throw new Error(
        `${path}: vtool reported no macOS deployment target; refusing to build`,
      );
    }
    for (const deploymentTarget of deploymentTargets) {
      if (compareVersions(deploymentTarget, expectedMinimumSystemVersion) > 0) {
        throw new Error(
          `${path}: Mach-O deployment target ${deploymentTarget} exceeds the ` +
            `declared macOS minimum ${expectedMinimumSystemVersion}`,
        );
      }
    }
    machOFiles.push(path);
  }

  if (machOFiles.length === 0) {
    throw new Error(`${input}: no Mach-O files found; refusing to build`);
  }

  log(
    `[verify-macos-compatibility] ${machOFiles.length} Mach-O files support ` +
      `macOS ${expectedMinimumSystemVersion}`,
  );
  return { input, machOCount: machOFiles.length, skipped: false };
}

export function extractDeploymentTargets(vtoolOutput) {
  const targets = [];
  let loadCommand = null;
  let platform = null;
  for (const line of String(vtoolOutput).split("\n")) {
    const value = line.trim();
    const commandMatch = /^cmd\s+(LC_[A-Z0-9_]+)$/.exec(value);
    if (commandMatch) {
      loadCommand = commandMatch[1];
      platform = null;
      continue;
    }
    if (loadCommand === "LC_BUILD_VERSION") {
      const platformMatch = /^platform\s+(\S+)$/.exec(value);
      if (platformMatch) {
        platform = platformMatch[1];
        continue;
      }
      const minimumMatch = /^minos\s+(\d+(?:\.\d+){1,2})$/.exec(value);
      if (platform === "MACOS" && minimumMatch) targets.push(minimumMatch[1]);
    } else if (loadCommand === "LC_VERSION_MIN_MACOSX") {
      const minimumMatch = /^version\s+(\d+(?:\.\d+){1,2})$/.exec(value);
      if (minimumMatch) targets.push(minimumMatch[1]);
    }
  }
  return targets;
}

async function collectFiles(path) {
  let info;
  try {
    info = await lstat(path);
  } catch (error) {
    throw new Error(`build compatibility input does not exist: ${path}`, {
      cause: error,
    });
  }
  if (info.isFile()) return [path];
  if (!info.isDirectory()) {
    throw new Error(`build compatibility input is not a regular file or directory: ${path}`);
  }

  const files = [];
  const entries = await readdir(path, { withFileTypes: true });
  entries.sort((left, right) => left.name.localeCompare(right.name));
  for (const entry of entries) {
    const child = join(path, entry.name);
    if (entry.isDirectory()) {
      files.push(...(await collectFiles(child)));
    } else if (entry.isFile()) {
      files.push(child);
    }
  }
  return files;
}

function compareVersions(left, right) {
  const leftParts = parseVersion(left, "Mach-O deployment target");
  const rightParts = parseVersion(right, "expected macOS minimum system version");
  const length = Math.max(leftParts.length, rightParts.length);
  for (let index = 0; index < length; index += 1) {
    const difference = (leftParts[index] ?? 0) - (rightParts[index] ?? 0);
    if (difference !== 0) return Math.sign(difference);
  }
  return 0;
}

function parseVersion(value, label) {
  if (!/^\d+(?:\.\d+){1,2}$/.test(value ?? "")) {
    throw new Error(`invalid ${label}: ${value ?? ""}`);
  }
  return value.split(".").map(Number);
}

async function commandText(runCommand, command, args) {
  const result = await runCommand(command, args);
  return String(result?.stdout ?? result ?? "").trim();
}

function defaultRunCommand(command, args) {
  return new Promise((resolveCommand, rejectCommand) => {
    const child = spawn(command, args, {
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
          `${command} ${args.join(" ")} failed with ${
            signal ? `signal ${signal}` : `exit ${code}`
          }\n${result.stderr || result.stdout}`,
        ),
      );
    });
  });
}

function isMainModule(metaUrl, argvPath = process.argv[1]) {
  if (!argvPath) return false;
  try {
    return realpathSync(fileURLToPath(metaUrl)) === realpathSync(argvPath);
  } catch {
    return false;
  }
}

if (isMainModule(import.meta.url)) {
  const [inputPath, expectedMinimumSystemVersion, ...extra] = process.argv.slice(2);
  if (!inputPath || extra.length > 0) {
    console.error(
      "Usage: node scripts/verify-macos-compatibility.mjs <file-or-directory> [minimum-macOS]",
    );
    process.exitCode = 2;
  } else {
    try {
      await verifyMacOSCompatibility(
        inputPath,
        expectedMinimumSystemVersion ?? declaredMacOSMinimumSystemVersion(),
      );
    } catch (error) {
      console.error(`[verify-macos-compatibility] ${error.message}`);
      process.exitCode = 1;
    }
  }
}
