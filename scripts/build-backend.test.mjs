import assert from "node:assert/strict";
import {
  mkdirSync,
  mkdtempSync,
  readFileSync,
  realpathSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, test } from "node:test";

import { buildBackend } from "./build-backend.mjs";

const temporaryDirectories = [];

afterEach(() => {
  for (const directory of temporaryDirectories.splice(0)) {
    rmSync(directory, { recursive: true, force: true });
  }
});

function repositoryFixture(platform = "darwin") {
  const root = mkdtempSync(join(tmpdir(), "build-backend-"));
  temporaryDirectories.push(root);
  const backend = join(root, "backend");
  const python = join(
    backend,
    "venv",
    platform === "win32" ? "Scripts/python.exe" : "bin/python",
  );
  mkdirSync(join(python, ".."), { recursive: true });
  writeFileSync(python, "python fixture");
  mkdirSync(join(root, "desktop-tauri", "src-tauri"), { recursive: true });
  writeFileSync(
    join(root, "desktop-tauri", "src-tauri", "tauri.conf.json"),
    JSON.stringify({ bundle: { macOS: { minimumSystemVersion: "11.0" } } }),
  );
  return { backend, python, root };
}

test("macOS build checks Python before PyInstaller and its output afterwards", async () => {
  const fixture = repositoryFixture();
  const commands = [];
  const compatibilityChecks = [];

  const result = await buildBackend({
    repositoryRoot: fixture.root,
    platform: "darwin",
    environment: {},
    runCommand: async (command, args, options) => {
      commands.push({ args, command, options });
    },
    verifyCompatibility: async (path, target, options) => {
      compatibilityChecks.push({ options, path, target });
    },
    log: () => {},
  });

  assert.equal(commands.length, 1);
  assert.equal(commands[0].command, fixture.python);
  assert.deepEqual(commands[0].args, [
    "-m",
    "PyInstaller",
    "suxiaoyou.spec",
    "--noconfirm",
    "--clean",
  ]);
  assert.equal(commands[0].options.cwd, fixture.backend);
  assert.equal(commands[0].options.env.MACOSX_DEPLOYMENT_TARGET, "11.0");
  assert.equal(compatibilityChecks.length, 2);
  assert.equal(compatibilityChecks[0].path, realpathSync(fixture.python));
  assert.equal(
    compatibilityChecks[1].path,
    join(fixture.backend, "dist", "suxiaoyou-backend"),
  );
  assert.equal(result.minimumSystemVersion, "11.0");
});

test("an incompatible Python fails before PyInstaller can produce output", async () => {
  const fixture = repositoryFixture();
  let commandCalled = false;

  await assert.rejects(
    buildBackend({
      repositoryRoot: fixture.root,
      platform: "darwin",
      environment: {},
      runCommand: async () => {
        commandCalled = true;
      },
      verifyCompatibility: async () => {
        throw new Error("Python deployment target 15.0 exceeds 11.0");
      },
      log: () => {},
    }),
    /Python deployment target 15\.0 exceeds 11\.0.*uv venv --python 3\.12\.13 --managed-python/s,
  );
  assert.equal(commandCalled, false);
});

test("an incompatible PyInstaller output fails the build after packaging", async () => {
  const fixture = repositoryFixture();
  let checks = 0;

  await assert.rejects(
    buildBackend({
      repositoryRoot: fixture.root,
      platform: "darwin",
      environment: {},
      runCommand: async () => {},
      verifyCompatibility: async () => {
        checks += 1;
        if (checks === 2) throw new Error("libcrypto minOS 26.0");
      },
      log: () => {},
    }),
    /libcrypto minOS 26\.0.*uv venv --python 3\.12\.13 --managed-python/s,
  );
  assert.equal(checks, 2);
});

test("Windows uses its venv Python and still validates that output exists", async () => {
  const fixture = repositoryFixture("win32");
  const commands = [];
  const checks = [];

  await buildBackend({
    repositoryRoot: fixture.root,
    platform: "win32",
    environment: {},
    runCommand: async (command, args, options) => {
      commands.push({ args, command, options });
    },
    verifyCompatibility: async (path, target, options) => {
      checks.push({ options, path, target });
    },
    log: () => {},
  });

  assert.equal(commands[0].command, fixture.python);
  assert.equal(commands[0].options.env.MACOSX_DEPLOYMENT_TARGET, undefined);
  assert.equal(checks.length, 1);
  assert.equal(checks[0].options.platform, "win32");
});

test("explicit backend Python takes priority over both conventional venvs", async () => {
  const fixture = repositoryFixture();
  const explicitPython = join(fixture.root, "portable-python", "bin", "python");
  const fallbackPython = join(fixture.backend, ".venv", "bin", "python");
  mkdirSync(join(explicitPython, ".."), { recursive: true });
  mkdirSync(join(fallbackPython, ".."), { recursive: true });
  writeFileSync(explicitPython, "portable python");
  writeFileSync(fallbackPython, "fallback python");
  const commands = [];

  await buildBackend({
    repositoryRoot: fixture.root,
    platform: "darwin",
    environment: { SUXIAOYOU_BACKEND_PYTHON: explicitPython },
    runCommand: async (command) => commands.push(command),
    verifyCompatibility: async () => {},
    log: () => {},
  });

  assert.deepEqual(commands, [explicitPython]);
});

test("falls back from backend/venv to backend/.venv", async () => {
  const fixture = repositoryFixture();
  rmSync(join(fixture.backend, "venv"), { recursive: true, force: true });
  const fallbackPython = join(fixture.backend, ".venv", "bin", "python");
  mkdirSync(join(fallbackPython, ".."), { recursive: true });
  writeFileSync(fallbackPython, "fallback python");
  const commands = [];

  await buildBackend({
    repositoryRoot: fixture.root,
    platform: "darwin",
    environment: {},
    runCommand: async (command) => commands.push(command),
    verifyCompatibility: async () => {},
    log: () => {},
  });

  assert.deepEqual(commands, [fallbackPython]);
});

test("missing or invalid Python fails closed with managed-runtime guidance", async () => {
  const fixture = repositoryFixture();
  rmSync(join(fixture.backend, "venv"), { recursive: true, force: true });

  await assert.rejects(
    buildBackend({
      repositoryRoot: fixture.root,
      platform: "darwin",
      environment: {},
      log: () => {},
    }),
    /no backend build Python.*uv venv --python 3\.12\.13 --managed-python.*SUXIAOYOU_BACKEND_PYTHON/s,
  );
  await assert.rejects(
    buildBackend({
      repositoryRoot: fixture.root,
      platform: "darwin",
      environment: { SUXIAOYOU_BACKEND_PYTHON: "missing/python" },
      log: () => {},
    }),
    /SUXIAOYOU_BACKEND_PYTHON points to a missing executable.*managed-python/s,
  );
});

test("package contract routes local backend builds through the guard", () => {
  const packageJson = JSON.parse(
    readFileSync(new URL("../package.json", import.meta.url), "utf8"),
  );
  const source = readFileSync(new URL("./build-backend.mjs", import.meta.url), "utf8");

  assert.equal(packageJson.scripts["build:backend"], "node scripts/build-backend.mjs");
  assert.match(source, /MACOSX_DEPLOYMENT_TARGET: minimumSystemVersion/);
  assert.match(source, /verifyCompatibility\(realpathSync\(python\)/);
  assert.match(source, /verifyCompatibility\(bundle, minimumSystemVersion/);
});
