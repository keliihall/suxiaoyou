import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import {
  copyFileSync,
  existsSync,
  mkdtempSync,
  mkdirSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";

const PREPARE_MODULE_URL = new URL(
  "../frontend/scripts/prepare-pdfjs-assets.mjs",
  import.meta.url,
);

function writeFixtureFile(rootDir, relativePath, contents) {
  const filePath = path.join(rootDir, relativePath);
  mkdirSync(path.dirname(filePath), { recursive: true });
  writeFileSync(filePath, contents);
}

function createLogger() {
  const info = [];
  const warnings = [];

  return {
    info,
    warnings,
    logger: {
      info(message) {
        info.push(message);
      },
      warn(message) {
        warnings.push(message);
      },
    },
  };
}

test("copies every PDF.js asset through paths containing spaces and Chinese characters", async (t) => {
  const frontendDir = mkdtempSync(path.join(tmpdir(), "苏小有 PDF.js assets "));
  t.after(() => rmSync(frontendDir, { recursive: true, force: true }));

  writeFixtureFile(
    frontendDir,
    "node_modules/pdfjs-dist/build/pdf.worker.min.mjs",
    "worker-v1",
  );
  writeFixtureFile(
    frontendDir,
    "node_modules/pdfjs-dist/cmaps/Adobe-GB1.bcmap",
    "cmap-v1",
  );
  writeFixtureFile(
    frontendDir,
    "node_modules/pdfjs-dist/standard_fonts/FoxitSans.pfb",
    "font-v1",
  );

  const { preparePdfjsAssets } = await import(PREPARE_MODULE_URL);
  const logs = createLogger();
  const report = preparePdfjsAssets({ frontendDir, logger: logs.logger });

  assert.equal(
    readFileSync(path.join(frontendDir, "public/pdf.worker.min.mjs"), "utf8"),
    "worker-v1",
  );
  assert.equal(
    readFileSync(path.join(frontendDir, "public/cmaps/Adobe-GB1.bcmap"), "utf8"),
    "cmap-v1",
  );
  assert.equal(
    readFileSync(
      path.join(frontendDir, "public/standard_fonts/FoxitSans.pfb"),
      "utf8",
    ),
    "font-v1",
  );
  assert.deepEqual(report, {
    copied: ["pdf.worker.min.mjs", "cmaps", "standard_fonts"],
    skipped: [],
  });
  assert.equal(logs.info.length, 3);
  assert.deepEqual(logs.warnings, []);
});

test("removes stale PDF.js destinations before copying replacements", async (t) => {
  const frontendDir = mkdtempSync(path.join(tmpdir(), "pdfjs-cleanup-"));
  t.after(() => rmSync(frontendDir, { recursive: true, force: true }));

  writeFixtureFile(
    frontendDir,
    "node_modules/pdfjs-dist/build/pdf.worker.min.mjs",
    "current-worker",
  );
  writeFixtureFile(
    frontendDir,
    "node_modules/pdfjs-dist/cmaps/current.bcmap",
    "current-cmap",
  );
  writeFixtureFile(
    frontendDir,
    "node_modules/pdfjs-dist/standard_fonts/current.pfb",
    "current-font",
  );
  writeFixtureFile(frontendDir, "public/pdf.worker.min.mjs", "stale-worker");
  writeFixtureFile(frontendDir, "public/cmaps/stale.bcmap", "stale-cmap");
  writeFixtureFile(frontendDir, "public/standard_fonts/stale.pfb", "stale-font");

  const { preparePdfjsAssets } = await import(PREPARE_MODULE_URL);
  preparePdfjsAssets({ frontendDir, logger: createLogger().logger });

  assert.equal(
    readFileSync(path.join(frontendDir, "public/pdf.worker.min.mjs"), "utf8"),
    "current-worker",
  );
  assert.equal(existsSync(path.join(frontendDir, "public/cmaps/stale.bcmap")), false);
  assert.equal(
    existsSync(path.join(frontendDir, "public/standard_fonts/stale.pfb")),
    false,
  );
});

test("reports all missing PDF.js sources without changing existing destinations", async (t) => {
  const frontendDir = mkdtempSync(path.join(tmpdir(), "pdfjs-missing-"));
  t.after(() => rmSync(frontendDir, { recursive: true, force: true }));

  writeFixtureFile(
    frontendDir,
    "node_modules/pdfjs-dist/build/pdf.worker.min.mjs",
    "current-worker",
  );
  writeFixtureFile(frontendDir, "public/pdf.worker.min.mjs", "stale-worker");
  writeFixtureFile(frontendDir, "public/cmaps/stale.bcmap", "stale-cmap");
  writeFixtureFile(frontendDir, "public/standard_fonts/stale.pfb", "stale-font");

  const { preparePdfjsAssets } = await import(PREPARE_MODULE_URL);
  const logs = createLogger();

  assert.throws(
    () => preparePdfjsAssets({ frontendDir, logger: logs.logger }),
    (error) => {
      assert.match(error.message, /PDF\.js asset validation failed/);
      assert.doesNotMatch(error.message, /pdf\.worker\.min\.mjs.*missing/is);
      assert.match(error.message, /cmaps.*missing.*directory/is);
      assert.match(error.message, /standard_fonts.*missing.*directory/is);
      return true;
    },
  );
  assert.equal(
    readFileSync(path.join(frontendDir, "public/pdf.worker.min.mjs"), "utf8"),
    "stale-worker",
  );
  assert.equal(
    readFileSync(path.join(frontendDir, "public/cmaps/stale.bcmap"), "utf8"),
    "stale-cmap",
  );
  assert.equal(
    readFileSync(
      path.join(frontendDir, "public/standard_fonts/stale.pfb"),
      "utf8",
    ),
    "stale-font",
  );
  assert.deepEqual(logs.info, []);
  assert.deepEqual(logs.warnings, []);
});

test("aggregates wrong source types and preserves every destination", async (t) => {
  const frontendDir = mkdtempSync(path.join(tmpdir(), "pdfjs-wrong-type-"));
  t.after(() => rmSync(frontendDir, { recursive: true, force: true }));

  mkdirSync(
    path.join(frontendDir, "node_modules/pdfjs-dist/build/pdf.worker.min.mjs"),
    { recursive: true },
  );
  writeFixtureFile(frontendDir, "node_modules/pdfjs-dist/cmaps", "not-a-directory");
  writeFixtureFile(frontendDir, "public/pdf.worker.min.mjs", "stale-worker");
  writeFixtureFile(frontendDir, "public/cmaps/stale.bcmap", "stale-cmap");
  writeFixtureFile(frontendDir, "public/standard_fonts/stale.pfb", "stale-font");

  const { preparePdfjsAssets } = await import(PREPARE_MODULE_URL);

  assert.throws(
    () => preparePdfjsAssets({ frontendDir, logger: createLogger().logger }),
    (error) => {
      assert.match(error.message, /pdf\.worker\.min\.mjs.*expected file/is);
      assert.match(error.message, /cmaps.*expected directory/is);
      assert.match(error.message, /standard_fonts.*missing.*directory/is);
      return true;
    },
  );
  assert.equal(
    readFileSync(path.join(frontendDir, "public/pdf.worker.min.mjs"), "utf8"),
    "stale-worker",
  );
  assert.equal(
    readFileSync(path.join(frontendDir, "public/cmaps/stale.bcmap"), "utf8"),
    "stale-cmap",
  );
  assert.equal(
    readFileSync(
      path.join(frontendDir, "public/standard_fonts/stale.pfb"),
      "utf8",
    ),
    "stale-font",
  );
});

test("direct execution exits nonzero when asset validation fails", (t) => {
  const frontendDir = mkdtempSync(path.join(tmpdir(), "苏小有 pdfjs CLI "));
  t.after(() => rmSync(frontendDir, { recursive: true, force: true }));

  const scriptPath = path.join(
    frontendDir,
    "scripts",
    "prepare-pdfjs-assets.mjs",
  );
  mkdirSync(path.dirname(scriptPath), { recursive: true });
  copyFileSync(PREPARE_MODULE_URL, scriptPath);
  writeFixtureFile(frontendDir, "public/pdf.worker.min.mjs", "stale-worker");

  const result = spawnSync(process.execPath, [scriptPath], {
    encoding: "utf8",
  });

  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /PDF\.js asset validation failed/);
  assert.equal(
    readFileSync(path.join(frontendDir, "public/pdf.worker.min.mjs"), "utf8"),
    "stale-worker",
  );
});

test("frontend lifecycle scripts prepare PDF.js assets without POSIX shell commands", () => {
  const packageJsonPath = new URL("../frontend/package.json", import.meta.url);
  const packageJson = JSON.parse(readFileSync(packageJsonPath, "utf8"));

  assert.equal(
    packageJson.scripts.prebuild,
    "node scripts/prepare-pdfjs-assets.mjs",
  );
  assert.equal(
    packageJson.scripts.postinstall,
    "node scripts/prepare-pdfjs-assets.mjs",
  );
  assert.equal(packageJson.scripts.build, "next build");

  const lifecycleCommands = [
    packageJson.scripts.prebuild,
    packageJson.scripts.postinstall,
    packageJson.scripts.build,
  ].join("\n");

  assert.doesNotMatch(lifecycleCommands, /(^|[\s;&|])cp([\s;&|]|$)/m);
  assert.doesNotMatch(lifecycleCommands, /\brm\s+-rf\b/);
  assert.doesNotMatch(lifecycleCommands, /\/dev\/null/);
  assert.doesNotMatch(lifecycleCommands, /[;&|]{1,2}/);
});
