#!/usr/bin/env node

import { spawn } from "node:child_process";
import {
  existsSync,
  readFileSync,
  realpathSync,
} from "node:fs";
import { readdir } from "node:fs/promises";
import { basename, dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const scriptDirectory = dirname(fileURLToPath(import.meta.url));
const repositoryRoot = resolve(scriptDirectory, "..");
const verifyBundleScript = join(scriptDirectory, "verify-bundle.mjs");
const expectedProductVersion = JSON.parse(
  readFileSync(join(repositoryRoot, "package.json"), "utf8"),
).version;
const nodeDownloader = readFileSync(
  join(repositoryRoot, "backend/scripts/download_node.py"),
  "utf8",
);
const nodeVersionMatch = /^NODE_VERSION\s*=\s*["']([^"']+)["']/m.exec(nodeDownloader);
if (!nodeVersionMatch) {
  throw new Error("cannot read NODE_VERSION from backend/scripts/download_node.py");
}

const EXPECTED_NODE_VERSION = `v${nodeVersionMatch[1]}`;
const EXPECTED_BUNDLE_IDENTIFIER = "com.chaoyuanxinzhi.suxiaoyou";
const EXPECTED_PRODUCT_NAME = "苏小有";
const SUPPORTED_ARCHITECTURES = new Set(["arm64", "x86_64"]);
const DISALLOWED_BUNDLE_XATTRS = Object.freeze([
  "com.apple.FinderInfo",
  "com.apple.ResourceFork",
]);

export const REQUIRED_RELEASE_LICENSE_FILES = Object.freeze([
  "licenses/LICENSE",
  "licenses/NOTICE",
  "licenses/THIRD_PARTY_NOTICES.md",
  "licenses/third-party/ANTHROPIC-CANVAS-FONTS-OFL-1.1.txt",
  "licenses/third-party/ANTHROPIC-KNOWLEDGE-WORK-PLUGINS-APACHE-2.0.txt",
  "licenses/third-party/ANTHROPIC-SKILLS-APACHE-2.0.txt",
  "licenses/third-party/CDLA-PERMISSIVE-2.0.txt",
  "licenses/third-party/COLORAMA-0.4.6-LICENSE.txt",
  "licenses/third-party/CPYTHON-3.12.13-LICENSE.txt",
  "licenses/third-party/JAVASCRIPT-LICENSES.txt",
  "licenses/third-party/MOZILLA-PUBLIC-LICENSE-2.0.txt",
  "licenses/third-party/NANOBOT-MIT.txt",
  "licenses/third-party/NODEJS-22.22.0-LICENSE.txt",
  "licenses/third-party/OPENCLAW-MIT.txt",
  "licenses/third-party/PYINSTALLER-6.21.0-COPYING.txt",
  "licenses/third-party/PYTHON-LICENSES.txt",
  "licenses/third-party/PYWIN32-312-LICENSES.txt",
  "licenses/third-party/README.md",
  "licenses/third-party/SOURCE_AVAILABILITY.md",
  "licenses/third-party/SUXIAOYOU-CJK-FONT-OFL-1.1.txt",
  "licenses/third-party/TENCENT-WEIXIN-OPENCLAW-1.0.3-MIT.txt",
  "licenses/third-party/TQDM-4.68.4-LICENSE.txt",
  "licenses/third-party/WEBENCODINGS-0.5.1-BSD-3-CLAUSE.txt",
  "licenses/third-party/RUST-LICENSES.html",
  "licenses/third-party/SHADCN-UI-MIT.txt",
  "licenses/third-party/python-runtime/python-build-standalone-20260623/README.md",
  "licenses/third-party/python-runtime/python-build-standalone-20260623/checksums.sha256",
  "licenses/third-party/python-runtime/python-build-standalone-20260623/build-system/LICENSE.python-build-standalone.MPL-2.0.txt",
  "licenses/third-party/python-runtime/python-build-standalone-20260623/metadata/PYTHON-aarch64-apple-darwin.json",
  "licenses/third-party/python-runtime/python-build-standalone-20260623/metadata/PYTHON-x86_64-apple-darwin.json",
  "licenses/third-party/python-runtime/python-build-standalone-20260623/licenses/LICENSE.bdb.txt",
  "licenses/third-party/python-runtime/python-build-standalone-20260623/licenses/LICENSE.bzip2.txt",
  "licenses/third-party/python-runtime/python-build-standalone-20260623/licenses/LICENSE.cpython.txt",
  "licenses/third-party/python-runtime/python-build-standalone-20260623/licenses/LICENSE.expat.txt",
  "licenses/third-party/python-runtime/python-build-standalone-20260623/licenses/LICENSE.libX11.txt",
  "licenses/third-party/python-runtime/python-build-standalone-20260623/licenses/LICENSE.libXau.txt",
  "licenses/third-party/python-runtime/python-build-standalone-20260623/licenses/LICENSE.libedit.txt",
  "licenses/third-party/python-runtime/python-build-standalone-20260623/licenses/LICENSE.libffi.txt",
  "licenses/third-party/python-runtime/python-build-standalone-20260623/licenses/LICENSE.liblzma.txt",
  "licenses/third-party/python-runtime/python-build-standalone-20260623/licenses/LICENSE.libuuid.txt",
  "licenses/third-party/python-runtime/python-build-standalone-20260623/licenses/LICENSE.libxcb.txt",
  "licenses/third-party/python-runtime/python-build-standalone-20260623/licenses/LICENSE.mpdecimal.txt",
  "licenses/third-party/python-runtime/python-build-standalone-20260623/licenses/LICENSE.ncurses.txt",
  "licenses/third-party/python-runtime/python-build-standalone-20260623/licenses/LICENSE.openssl-1.1.txt",
  "licenses/third-party/python-runtime/python-build-standalone-20260623/licenses/LICENSE.openssl-3.txt",
  "licenses/third-party/python-runtime/python-build-standalone-20260623/licenses/LICENSE.sqlite.txt",
  "licenses/third-party/python-runtime/python-build-standalone-20260623/licenses/LICENSE.tcl.txt",
  "licenses/third-party/python-runtime/python-build-standalone-20260623/licenses/LICENSE.tix.txt",
  "licenses/third-party/python-runtime/python-build-standalone-20260623/licenses/LICENSE.zlib.txt",
]);

export async function verifyMacOSBundle(
  appPath,
  expectedArchitecture,
  expectedMinimumSystemVersion,
  {
    runCommand = defaultRunCommand,
    log = console.log,
    verifySignature = false,
    skipBackendSmoke = false,
  } = {},
) {
  const app = resolve(appPath);
  if (!SUPPORTED_ARCHITECTURES.has(expectedArchitecture)) {
    throw new Error(
      `unsupported architecture "${expectedArchitecture}"; expected arm64 or x86_64`,
    );
  }
  parseVersion(expectedMinimumSystemVersion, "expected minimum system version");
  requirePath(app, "app bundle");

  const contents = join(app, "Contents");
  const resources = join(contents, "Resources");
  const infoPlist = join(contents, "Info.plist");
  const backend = join(resources, "backend");
  const backendExecutable = join(backend, "suxiaoyou-backend");
  const nodeBinary = join(resources, "nodejs", "bin", "node");
  requirePath(infoPlist, "Info.plist");
  requirePath(backend, "embedded backend");
  requirePath(backendExecutable, "embedded backend executable");
  requirePath(nodeBinary, "bundled Node binary");
  for (const relativePath of REQUIRED_RELEASE_LICENSE_FILES) {
    requirePath(
      join(resources, relativePath),
      `bundled release license ${relativePath}`,
    );
  }

  const recursiveXattrs = await commandText(runCommand, "xattr", ["-lr", app]);
  for (const attribute of DISALLOWED_BUNDLE_XATTRS) {
    if (recursiveXattrs.includes(`${attribute}:`)) {
      throw new Error(
        `app bundle contains disallowed extended attribute ${attribute}`,
      );
    }
  }

  const info = {};
  for (const key of [
    "CFBundleIdentifier",
    "CFBundleExecutable",
    "CFBundleName",
    "CFBundleShortVersionString",
    "LSMinimumSystemVersion",
  ]) {
    info[key] = await commandText(runCommand, "plutil", ["-extract", key, "raw", infoPlist]);
  }
  requireEqual(info.CFBundleIdentifier, EXPECTED_BUNDLE_IDENTIFIER, "bundle identifier");
  if (basename(info.CFBundleExecutable) !== info.CFBundleExecutable) {
    throw new Error(`invalid CFBundleExecutable: ${info.CFBundleExecutable}`);
  }
  const appExecutable = join(contents, "MacOS", info.CFBundleExecutable);
  requirePath(appExecutable, "app executable");
  requireEqual(info.CFBundleName, EXPECTED_PRODUCT_NAME, "bundle name");
  requireEqual(info.CFBundleShortVersionString, expectedProductVersion, "bundle version");
  requireEqual(
    info.LSMinimumSystemVersion,
    expectedMinimumSystemVersion,
    "Info.plist minimum system version",
  );

  const files = await collectRegularFiles(app);
  const machOFiles = [];
  for (const path of files) {
    const fileDescription = await commandText(runCommand, "file", ["-b", path]);
    if (!fileDescription.includes("Mach-O")) continue;

    const architectures = (
      await commandText(runCommand, "lipo", ["-archs", path])
    ).split(/\s+/).filter(Boolean);
    if (architectures.length !== 1 || architectures[0] !== expectedArchitecture) {
      throw new Error(
        `${path}: expected exactly ${expectedArchitecture}, got ${architectures.join(" ") || "none"}`,
      );
    }

    const buildMetadata = await commandText(runCommand, "vtool", ["-show-build", path]);
    const deploymentTargets = extractDeploymentTargets(buildMetadata);
    if (deploymentTargets.length === 0) {
      throw new Error(`${path}: vtool reported no macOS deployment target`);
    }
    for (const deploymentTarget of deploymentTargets) {
      if (compareVersions(deploymentTarget, expectedMinimumSystemVersion) > 0) {
        throw new Error(
          `${path}: deployment target ${deploymentTarget} exceeds ${expectedMinimumSystemVersion}`,
        );
      }
    }
    machOFiles.push(path);
  }
  if (machOFiles.length === 0) {
    throw new Error(`no Mach-O files found in ${app}`);
  }
  if (!machOFiles.includes(appExecutable)) {
    throw new Error(`declared app executable is not a regular Mach-O: ${appExecutable}`);
  }
  if (!machOFiles.includes(nodeBinary)) {
    throw new Error(`bundled Node was not recognized as Mach-O: ${nodeBinary}`);
  }

  const nodeVersion = await commandText(runCommand, nodeBinary, ["--version"]);
  if (nodeVersion !== EXPECTED_NODE_VERSION) {
    throw new Error(`expected Node ${EXPECTED_NODE_VERSION}, got ${nodeVersion}`);
  }
  const expectedNodeArchitecture = expectedArchitecture === "x86_64" ? "x64" : "arm64";
  const nodeArchitecture = await commandText(runCommand, nodeBinary, ["-p", "process.arch"]);
  if (nodeArchitecture !== expectedNodeArchitecture) {
    throw new Error(
      `expected Node process.arch ${expectedNodeArchitecture}, got ${nodeArchitecture}`,
    );
  }

  if (verifySignature) {
    await runCommand("codesign", [
      "--verify",
      "--deep",
      "--strict",
      "--verbose=2",
      app,
    ]);
  }

  const backendHelp = await commandText(runCommand, backendExecutable, ["--help"]);
  if (!backendHelp.includes("--port") || !backendHelp.includes("--data-dir")) {
    throw new Error("embedded backend --help output is incomplete");
  }

  await runCommand(process.execPath, [verifyBundleScript, backend], {
    env: {
      ...process.env,
      VERIFY_BUNDLE_SKIP_SMOKE: skipBackendSmoke ? "1" : "0",
    },
  });

  log(
    `[verify-macos-bundle] ${EXPECTED_PRODUCT_NAME} ${expectedProductVersion}: ` +
      `${machOFiles.length} Mach-O files are exactly ${expectedArchitecture}, ` +
      `minOS=${expectedMinimumSystemVersion}, Node=${nodeVersion}/${nodeArchitecture}, ` +
      `signature=${verifySignature ? "verified" : "not requested"}, ` +
      `${REQUIRED_RELEASE_LICENSE_FILES.length} license resources present, ` +
      "backend preflight=passed, " +
      `backend smoke=${skipBackendSmoke ? "explicitly skipped" : "passed"}`,
  );
  return {
    app,
    bundleIdentifier: info.CFBundleIdentifier,
    bundleVersion: info.CFBundleShortVersionString,
    minimumSystemVersion: info.LSMinimumSystemVersion,
    machOCount: machOFiles.length,
    nodeArchitecture,
    nodeVersion,
    signatureVerified: verifySignature,
    backendPreflightPassed: true,
    backendSmokeSkipped: skipBackendSmoke,
    releaseLicenseCount: REQUIRED_RELEASE_LICENSE_FILES.length,
  };
}

async function collectRegularFiles(directory) {
  const files = [];
  const entries = await readdir(directory, { withFileTypes: true });
  entries.sort((left, right) => left.name.localeCompare(right.name));
  for (const entry of entries) {
    const path = join(directory, entry.name);
    if (entry.isDirectory()) {
      files.push(...(await collectRegularFiles(path)));
    } else if (entry.isFile()) {
      files.push(path);
    }
  }
  return files;
}

function extractDeploymentTargets(vtoolOutput) {
  const targets = [];
  let loadCommand = null;
  let platform = null;
  for (const line of vtoolOutput.split("\n")) {
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

function compareVersions(left, right) {
  const leftParts = parseVersion(left, "deployment target");
  const rightParts = parseVersion(right, "expected minimum system version");
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

function requireEqual(actual, expected, label) {
  if (actual !== expected) {
    throw new Error(`expected ${label} ${expected}, got ${actual}`);
  }
}

function requirePath(path, label) {
  if (!existsSync(path)) {
    throw new Error(`${label} does not exist: ${path}`);
  }
}

async function commandText(runCommand, command, args) {
  const result = await runCommand(command, args);
  return String(result?.stdout ?? result ?? "").trim();
}

function defaultRunCommand(command, args, options = {}) {
  return new Promise((resolveCommand, rejectCommand) => {
    const child = spawn(command, args, {
      cwd: options.cwd,
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
          `${command} ${args.join(" ")} failed with ${signal ? `signal ${signal}` : `exit ${code}`}` +
            `\n${result.stderr || result.stdout}`,
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
  const [appPath, expectedArchitecture, expectedMinimumSystemVersion, ...flags] = process.argv.slice(2);
  const supportedFlags = new Set(["--verify-signature", "--skip-backend-smoke"]);
  const unknownFlags = flags.filter((flag) => !supportedFlags.has(flag));
  if (!appPath || !expectedArchitecture || !expectedMinimumSystemVersion) {
    console.error(
      "Usage: node scripts/verify-macos-bundle.mjs <app-path> <arm64|x86_64> <minimum-macOS> [--verify-signature] [--skip-backend-smoke]",
    );
    process.exitCode = 2;
  } else if (unknownFlags.length > 0) {
    console.error(`Unknown option: ${unknownFlags[0]}`);
    process.exitCode = 2;
  } else {
    try {
      await verifyMacOSBundle(appPath, expectedArchitecture, expectedMinimumSystemVersion, {
        verifySignature: flags.includes("--verify-signature"),
        skipBackendSmoke: flags.includes("--skip-backend-smoke"),
      });
    } catch (error) {
      console.error(`[verify-macos-bundle] ${error.message}`);
      process.exitCode = 1;
    }
  }
}
