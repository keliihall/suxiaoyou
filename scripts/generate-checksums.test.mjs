import assert from "node:assert/strict";
import { mkdtempSync, mkdirSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";
import test from "node:test";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const script = join(root, "scripts", "generate-checksums.mjs");

function fixture() {
  return mkdtempSync(join(tmpdir(), "suxiaoyou-checksums-"));
}

test("includes installer-only artifacts and excludes updater remnants", (t) => {
  const directory = fixture();
  t.after(() => rmSync(directory, { recursive: true, force: true }));
  mkdirSync(join(directory, "nested"));

  for (const name of [
    "苏小有_0.7.3_x64.exe",
    "苏小有_0.7.3_aarch64.dmg",
    "苏小有_0.7.3_x64.dmg",
    "苏小有_0.7.3_amd64.deb",
    "苏小有-0.7.3-1.x86_64.rpm",
    "苏小有_0.7.3_arm64.deb",
    "苏小有-0.7.3-1.aarch64.rpm",
    "苏小有.app.tar.gz",
    "苏小有.app.tar.gz.sig",
    "latest.json",
  ]) {
    writeFileSync(join(directory, name), name);
  }
  writeFileSync(join(directory, "nested", "notes.txt"), "ignored");

  const result = spawnSync(process.execPath, [script, directory], {
    encoding: "utf8",
  });

  assert.equal(result.status, 0, result.stderr);
  for (const extension of [".exe", ".dmg", ".deb", ".rpm"]) {
    assert.match(result.stdout, new RegExp(`\\${extension.replace(".", ".")}`));
  }
  assert.doesNotMatch(result.stdout, /tar\.gz|\.sig|latest\.json|notes\.txt/);
  assert.equal((result.stdout.match(/^\| `/gm) ?? []).length, 7);
  assert.match(result.stdout, /\d+\.\d MiB/);
  assert.doesNotMatch(result.stdout, /\d+\.\d MB/);
});

test("updater-only directories are rejected", (t) => {
  const directory = fixture();
  t.after(() => rmSync(directory, { recursive: true, force: true }));
  writeFileSync(join(directory, "苏小有.app.tar.gz"), "updater");
  writeFileSync(join(directory, "苏小有.app.tar.gz.sig"), "signature");

  const result = spawnSync(process.execPath, [script, directory], {
    encoding: "utf8",
  });

  assert.equal(result.status, 1);
  assert.match(result.stderr, /no installer artifacts/);
});
