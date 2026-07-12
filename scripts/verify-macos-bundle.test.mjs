import assert from "node:assert/strict";
import {
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  unlinkSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { basename, join } from "node:path";
import { afterEach, test } from "node:test";

import {
  REQUIRED_RELEASE_LICENSE_FILES,
  verifyMacOSBundle,
} from "./verify-macos-bundle.mjs";

const PRODUCT_VERSION = JSON.parse(
  readFileSync(new URL("../package.json", import.meta.url), "utf8"),
).version;
const temporaryDirectories = [];

afterEach(() => {
  for (const directory of temporaryDirectories.splice(0)) {
    rmSync(directory, { recursive: true, force: true });
  }
});

function appFixture() {
  const root = mkdtempSync(join(tmpdir(), "verify-macos-bundle-"));
  temporaryDirectories.push(root);
  const app = join(root, "苏小有.app");
  const resources = join(app, "Contents", "Resources");
  const backend = join(resources, "backend");
  const nodeDirectory = join(resources, "nodejs", "bin");
  const executableDirectory = join(app, "Contents", "MacOS");
  mkdirSync(join(backend, "_internal"), { recursive: true });
  mkdirSync(nodeDirectory, { recursive: true });
  mkdirSync(executableDirectory, { recursive: true });

  const info = join(app, "Contents", "Info.plist");
  const executable = join(executableDirectory, "suxiaoyou-desktop");
  const backendExecutable = join(backend, "suxiaoyou-backend");
  const node = join(nodeDirectory, "node");
  const npm = join(nodeDirectory, "npm");
  const npx = join(nodeDirectory, "npx");
  const text = join(resources, "README.txt");
  for (const path of [info, executable, backendExecutable, node, npm, npx, text]) {
    writeFileSync(path, basename(path));
  }
  const releaseLicenseFiles = REQUIRED_RELEASE_LICENSE_FILES.map((relativePath) =>
    join(resources, relativePath),
  );
  for (const path of releaseLicenseFiles) {
    mkdirSync(join(path, ".."), { recursive: true });
    writeFileSync(path, basename(path));
  }

  return {
    app,
    backend,
    executable,
    backendExecutable,
    info,
    node,
    npm,
    npx,
    releaseLicenseFiles,
    resources,
    text,
  };
}

function commandRunner(fixture, overrides = {}) {
  const calls = [];
  const machO = new Set([fixture.executable, fixture.backendExecutable, fixture.node]);
  const runCommand = async (command, args, options = {}) => {
    calls.push({ command, args, options });
    if (command === "plutil") {
      const key = args[1];
      return {
        stdout:
          {
            CFBundleIdentifier: "com.suxiaoyou.desktop",
            CFBundleExecutable: "suxiaoyou-desktop",
            CFBundleName: "苏小有",
            CFBundleShortVersionString: PRODUCT_VERSION,
            LSMinimumSystemVersion: overrides.infoMinOS ?? "13.3",
          }[key] + "\n",
        stderr: "",
      };
    }
    if (command === "file") {
      return {
        stdout: machO.has(args.at(-1)) ? "Mach-O 64-bit executable arm64\n" : "ASCII text\n",
        stderr: "",
      };
    }
    if (command === "lipo") {
      return { stdout: `${overrides.arch ?? "arm64"}\n`, stderr: "" };
    }
    if (command === "vtool") {
      return {
        stdout:
          "Load command 9\n" +
          "      cmd LC_BUILD_VERSION\n" +
          " platform MACOS\n" +
          `    minos ${overrides.binaryMinOS ?? "13.3"}\n` +
          "     tool LD\n" +
          "  version 1267.0\n",
        stderr: "",
      };
    }
    if (command === "xattr") {
      return { stdout: overrides.xattrs ?? "", stderr: "" };
    }
    if (command === fixture.node && args[0] === "--version") {
      return { stdout: `${overrides.nodeVersion ?? "v22.22.0"}\n`, stderr: "" };
    }
    if (command === fixture.node && args.join(" ") === "-p process.arch") {
      return { stdout: `${overrides.nodeArch ?? "arm64"}\n`, stderr: "" };
    }
    if (command === fixture.node && args.join(" ") === "-p process.execPath") {
      return { stdout: `${fixture.node}\n`, stderr: "" };
    }
    if (command === fixture.npm && args.join(" ") === "--version") {
      return { stdout: `${overrides.npmVersion ?? "10.9.4"}\n`, stderr: "" };
    }
    if (command === fixture.npx && args.join(" ") === "--version") {
      return { stdout: `${overrides.npxVersion ?? "10.9.4"}\n`, stderr: "" };
    }
    if (command === fixture.backendExecutable && args[0] === "--help") {
      if (overrides.backendHelpError) {
        throw new Error(overrides.backendHelpError);
      }
      return {
        stdout:
          overrides.backendHelp ??
          "usage: suxiaoyou-backend [-h] [--port PORT] [--data-dir DATA_DIR]\n",
        stderr: "",
      };
    }
    if (command === process.execPath && args[0].endsWith("verify-bundle.mjs")) {
      return { stdout: "bundle verified\n", stderr: "" };
    }
    if (command === "codesign") {
      return { stdout: "", stderr: "" };
    }
    throw new Error(`unexpected command: ${command} ${args.join(" ")}`);
  };
  return { calls, runCommand };
}

test("checks every Mach-O, Node, Info.plist, and the embedded backend", async () => {
  const fixture = appFixture();
  const runner = commandRunner(fixture);

  const result = await verifyMacOSBundle(fixture.app, "arm64", "13.3", {
    runCommand: runner.runCommand,
    log: () => {},
  });

  assert.equal(result.machOCount, 3);
  assert.equal(result.nodeVersion, "v22.22.0");
  assert.equal(result.npmVersion, "10.9.4");
  assert.equal(result.npxVersion, "10.9.4");
  assert.equal(result.bundleIdentifier, "com.suxiaoyou.desktop");
  assert.equal(result.backendPreflightPassed, true);
  assert.equal(result.releaseLicenseCount, REQUIRED_RELEASE_LICENSE_FILES.length);
  assert.equal(
    runner.calls.filter(({ command }) => command === "file").length,
    7 + REQUIRED_RELEASE_LICENSE_FILES.length,
    "every regular file must be classified with file(1)",
  );
  assert.equal(runner.calls.filter(({ command }) => command === "lipo").length, 3);
  assert.equal(runner.calls.filter(({ command }) => command === "vtool").length, 3);
  assert.ok(
    runner.calls.some(
      ({ command, args }) =>
        command === "xattr" && args.join(" ") === `-lr ${fixture.app}`,
    ),
  );
  assert.ok(
    runner.calls.some(
      ({ command, args }) =>
        command === fixture.backendExecutable && args.join(" ") === "--help",
    ),
  );
  assert.ok(
    runner.calls.some(
      ({ command, args }) =>
        command === process.execPath &&
        args[0].endsWith("verify-bundle.mjs") &&
        args[1] === fixture.backend,
    ),
  );
});

test("rejects an app that omits a mandatory release-license resource", async () => {
  const fixture = appFixture();
  unlinkSync(fixture.releaseLicenseFiles[0]);
  const runner = commandRunner(fixture);

  await assert.rejects(
    verifyMacOSBundle(fixture.app, "arm64", "13.3", {
      runCommand: runner.runCommand,
      log: () => {},
    }),
    /bundled release license licenses\/LICENSE does not exist/,
  );
});

test("can require a complete app-bundle signature", async () => {
  const fixture = appFixture();
  const runner = commandRunner(fixture);

  const result = await verifyMacOSBundle(fixture.app, "arm64", "13.3", {
    runCommand: runner.runCommand,
    log: () => {},
    verifySignature: true,
  });

  assert.equal(result.signatureVerified, true);
  assert.ok(
    runner.calls.some(
      ({ command, args }) =>
        command === "codesign" &&
        args.join(" ") === `--verify --deep --strict --verbose=2 ${fixture.app}`,
    ),
  );
});

test("can explicitly skip only the embedded backend smoke", async () => {
  const fixture = appFixture();
  const runner = commandRunner(fixture);

  const result = await verifyMacOSBundle(fixture.app, "arm64", "13.3", {
    runCommand: runner.runCommand,
    log: () => {},
    skipBackendSmoke: true,
  });

  assert.equal(result.backendSmokeSkipped, true);
  assert.equal(result.backendPreflightPassed, true);
  assert.ok(
    runner.calls.some(
      ({ command, args }) =>
        command === fixture.backendExecutable && args.join(" ") === "--help",
    ),
    "the no-port backend preflight must never be skipped",
  );
  const backendVerification = runner.calls.find(
    ({ command, args }) =>
      command === process.execPath && args[0].endsWith("verify-bundle.mjs"),
  );
  assert.equal(backendVerification.options.env.VERIFY_BUNDLE_SKIP_SMOKE, "1");
});

test("rejects a backend that cannot execute even when full smoke is skipped", async () => {
  const fixture = appFixture();
  const runner = commandRunner(fixture, {
    backendHelpError: "different Team IDs while loading libpython3.12.dylib",
  });

  await assert.rejects(
    verifyMacOSBundle(fixture.app, "arm64", "13.3", {
      runCommand: runner.runCommand,
      log: () => {},
      skipBackendSmoke: true,
    }),
    /different Team IDs.*libpython3\.12\.dylib/,
  );
  assert.equal(
    runner.calls.some(
      ({ command, args }) =>
        command === process.execPath && args[0].endsWith("verify-bundle.mjs"),
    ),
    false,
  );
});

for (const attribute of ["com.apple.FinderInfo", "com.apple.ResourceFork"]) {
  test(`rejects disallowed recursive xattr ${attribute}`, async () => {
    const fixture = appFixture();
    const runner = commandRunner(fixture, {
      xattrs: `${fixture.backendExecutable}: ${attribute}:\n00000000`,
    });

    await assert.rejects(
      verifyMacOSBundle(fixture.app, "arm64", "13.3", {
        runCommand: runner.runCommand,
        log: () => {},
      }),
      new RegExp(`disallowed extended attribute ${attribute.replaceAll(".", "\\.")}`),
    );
    assert.equal(
      runner.calls.some(
        ({ command, args }) =>
          command === fixture.backendExecutable && args.join(" ") === "--help",
      ),
      false,
    );
  });
}

test("rejects a Mach-O that is not exactly the requested architecture", async () => {
  const fixture = appFixture();
  const runner = commandRunner(fixture, { arch: "arm64 x86_64" });

  await assert.rejects(
    verifyMacOSBundle(fixture.app, "arm64", "13.3", {
      runCommand: runner.runCommand,
      log: () => {},
    }),
    /expected exactly arm64.*arm64 x86_64/s,
  );
});

test("rejects a Mach-O whose deployment target exceeds the declared minimum", async () => {
  const fixture = appFixture();
  const runner = commandRunner(fixture, { binaryMinOS: "14.0" });

  await assert.rejects(
    verifyMacOSBundle(fixture.app, "arm64", "13.3", {
      runCommand: runner.runCommand,
      log: () => {},
    }),
    /deployment target 14\.0 exceeds 13\.3/,
  );
});

test("accepts a native x86_64 app with an x64 Node runtime", async () => {
  const fixture = appFixture();
  const runner = commandRunner(fixture, { arch: "x86_64", nodeArch: "x64" });

  const result = await verifyMacOSBundle(fixture.app, "x86_64", "13.3", {
    runCommand: runner.runCommand,
    log: () => {},
  });

  assert.equal(result.nodeArchitecture, "x64");
  assert.equal(result.machOCount, 3);
});

test("rejects Node when process.arch does not match the app architecture", async () => {
  const fixture = appFixture();
  const runner = commandRunner(fixture, { nodeArch: "x64" });

  await assert.rejects(
    verifyMacOSBundle(fixture.app, "arm64", "13.3", {
      runCommand: runner.runCommand,
      log: () => {},
    }),
    /expected Node process\.arch arm64, got x64/,
  );
});

test("rejects the wrong bundled Node version before backend smoke", async () => {
  const fixture = appFixture();
  const runner = commandRunner(fixture, { nodeVersion: "v22.21.0" });

  await assert.rejects(
    verifyMacOSBundle(fixture.app, "arm64", "13.3", {
      runCommand: runner.runCommand,
      log: () => {},
    }),
    /expected Node v22\.22\.0.*v22\.21\.0/s,
  );
  assert.equal(
    runner.calls.some(
      ({ command, args }) => command === process.execPath && args[0].endsWith("verify-bundle.mjs"),
    ),
    false,
  );
});

test("rejects a broken bundled npm before backend smoke", async () => {
  const fixture = appFixture();
  const runner = commandRunner(fixture, { npmVersion: "broken" });

  await assert.rejects(
    verifyMacOSBundle(fixture.app, "arm64", "13.3", {
      runCommand: runner.runCommand,
      log: () => {},
    }),
    /invalid npm version output: broken/,
  );
  assert.equal(
    runner.calls.some(
      ({ command, args }) => command === process.execPath && args[0].endsWith("verify-bundle.mjs"),
    ),
    false,
  );
});

test("rejects an app whose declared main executable is missing", async () => {
  const fixture = appFixture();
  unlinkSync(fixture.executable);
  const runner = commandRunner(fixture);

  await assert.rejects(
    verifyMacOSBundle(fixture.app, "arm64", "13.3", {
      runCommand: runner.runCommand,
      log: () => {},
    }),
    /app executable does not exist/,
  );
});
