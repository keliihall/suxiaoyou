#!/usr/bin/env node

import { spawn } from "node:child_process";
import { existsSync, realpathSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import {
  declaredMacOSMinimumSystemVersion,
  verifyMacOSCompatibility,
} from "./verify-macos-compatibility.mjs";

const scriptDirectory = dirname(fileURLToPath(import.meta.url));
const defaultRepositoryRoot = resolve(scriptDirectory, "..");

export async function buildBackend({
  repositoryRoot = defaultRepositoryRoot,
  platform = process.platform,
  environment = process.env,
  runCommand = defaultRunCommand,
  verifyCompatibility = verifyMacOSCompatibility,
  log = console.log,
} = {}) {
  const backendDirectory = join(repositoryRoot, "backend");
  const python = resolveBackendPython(
    repositoryRoot,
    platform,
    environment,
  );

  const minimumSystemVersion = declaredMacOSMinimumSystemVersion(repositoryRoot);
  if (platform === "darwin") {
    // Catch an incompatible Homebrew/system Python before an expensive
    // PyInstaller run. The output scan below remains authoritative because
    // transitive libraries can have a newer target than the interpreter.
    try {
      await verifyCompatibility(realpathSync(python), minimumSystemVersion, {
        platform,
        log,
      });
    } catch (error) {
      throw new Error(`${error.message}\n${managedPythonGuidance()}`, {
        cause: error,
      });
    }
  }

  await runCommand(
    python,
    ["-m", "PyInstaller", "suxiaoyou.spec", "--noconfirm", "--clean"],
    {
      cwd: backendDirectory,
      env: {
        ...environment,
        ...(platform === "darwin"
          ? { MACOSX_DEPLOYMENT_TARGET: minimumSystemVersion }
          : {}),
      },
    },
  );

  const bundle = join(backendDirectory, "dist", "suxiaoyou-backend");
  try {
    await verifyCompatibility(bundle, minimumSystemVersion, { platform, log });
  } catch (error) {
    throw new Error(`${error.message}\n${managedPythonGuidance()}`, {
      cause: error,
    });
  }
  log(`[build-backend] verified backend bundle: ${bundle}`);
  return { bundle, minimumSystemVersion, python };
}

export function resolveBackendPython(
  repositoryRoot,
  platform = process.platform,
  environment = process.env,
) {
  const executable = platform === "win32" ? "Scripts/python.exe" : "bin/python";
  const explicit = String(environment.SUXIAOYOU_BACKEND_PYTHON ?? "").trim();
  if (explicit) {
    const path = resolve(repositoryRoot, explicit);
    if (!existsSync(path)) {
      throw missingPythonError(
        `SUXIAOYOU_BACKEND_PYTHON points to a missing executable: ${path}`,
      );
    }
    return path;
  }

  for (const environmentName of ["venv", ".venv"]) {
    const path = join(repositoryRoot, "backend", environmentName, executable);
    if (existsSync(path)) return path;
  }
  throw missingPythonError(
    `no backend build Python found under backend/venv or backend/.venv`,
  );
}

function missingPythonError(reason) {
  return new Error(`${reason}.\n${managedPythonGuidance()}`);
}

function managedPythonGuidance() {
  return (
    `Create the release-compatible Python 3.12.13 environment with:\n` +
    `  uv venv --python 3.12.13 --managed-python --seed backend/venv\n` +
    `Then install backend requirements and PyInstaller 6.21.0, or set ` +
    `SUXIAOYOU_BACKEND_PYTHON to that environment's Python executable.`
  );
}

function defaultRunCommand(command, args, options = {}) {
  return new Promise((resolveCommand, rejectCommand) => {
    const child = spawn(command, args, {
      cwd: options.cwd,
      env: options.env ?? process.env,
      stdio: "inherit",
    });
    child.on("error", rejectCommand);
    child.on("close", (code, signal) => {
      if (code === 0) {
        resolveCommand();
        return;
      }
      rejectCommand(
        new Error(
          `${command} failed with ${signal ? `signal ${signal}` : `exit ${code}`}`,
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
  try {
    await buildBackend();
  } catch (error) {
    console.error(`[build-backend] ${error.message}`);
    process.exitCode = 1;
  }
}
