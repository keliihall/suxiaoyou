import assert from "node:assert/strict";
import { execFile } from "node:child_process";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";
import test from "node:test";

const execFileAsync = promisify(execFile);
const legacyBrand = ["Open", "Yak"].join("");
const sanitizerPath = fileURLToPath(
  new URL("./sanitize-build-brand.mjs", import.meta.url),
);

async function withBuildDir(run) {
  const buildDir = await mkdtemp(join(tmpdir(), "sanitize-build-brand-"));
  try {
    await run(buildDir);
  } finally {
    await rm(buildDir, { recursive: true, force: true });
  }
}

async function sanitize(buildDir) {
  await execFileAsync(process.execPath, [sanitizerPath, buildDir]);
}

test("sanitizes recognized UTF-8 text files without rewriting legal notices", async () => {
  await withBuildDir(async (buildDir) => {
    const textPath = join(buildDir, "app.txt");
    const uppercaseHtmlPath = join(buildDir, "index.HTML");
    const licensePath = join(buildDir, "LICENSE_THIRD_PARTY");
    const noticePath = join(buildDir, "THIRD_PARTY_NOTICES.txt");
    await Promise.all([
      writeFile(textPath, `${legacyBrand} text`),
      writeFile(uppercaseHtmlPath, `${legacyBrand} page`),
      writeFile(licensePath, `${legacyBrand} license`),
      writeFile(noticePath, `${legacyBrand} notice`),
    ]);

    await sanitize(buildDir);

    assert.equal(await readFile(textPath, "utf8"), "suyo text");
    assert.equal(await readFile(uppercaseHtmlPath, "utf8"), "suyo page");
    assert.equal(await readFile(licensePath, "utf8"), `${legacyBrand} license`);
    assert.equal(await readFile(noticePath, "utf8"), `${legacyBrand} notice`);
  });
});

test("preserves a file with a NUL byte after the first 8192 bytes", async () => {
  await withBuildDir(async (buildDir) => {
    const lateNulPath = join(buildDir, "late-nul.js");
    const prefix = Buffer.from(legacyBrand);
    const originalLateNul = Buffer.concat([
      prefix,
      Buffer.alloc(8192 - prefix.length, 0x61),
      Buffer.from([0, 0x62]),
    ]);
    await writeFile(lateNulPath, originalLateNul);

    await sanitize(buildDir);

    assert.deepEqual(await readFile(lateNulPath), originalLateNul);
  });
});

test("preserves a file containing invalid UTF-8", async () => {
  await withBuildDir(async (buildDir) => {
    const invalidUtf8Path = join(buildDir, "invalid.js");
    const originalInvalidUtf8 = Buffer.concat([
      Buffer.from(`${legacyBrand} `),
      Buffer.from([0xc3, 0x28]),
    ]);
    await writeFile(invalidUtf8Path, originalInvalidUtf8);

    await sanitize(buildDir);

    assert.deepEqual(await readFile(invalidUtf8Path), originalInvalidUtf8);
  });
});

test("preserves an unknown extensionless file with printable bytes", async () => {
  await withBuildDir(async (buildDir) => {
    const unknownExtensionlessPath = join(buildDir, "artifact");
    const originalPrintableBytes = Buffer.from(
      `${legacyBrand} printable bytes`,
    );
    await writeFile(unknownExtensionlessPath, originalPrintableBytes);

    await sanitize(buildDir);

    assert.deepEqual(
      await readFile(unknownExtensionlessPath),
      originalPrintableBytes,
    );
  });
});
