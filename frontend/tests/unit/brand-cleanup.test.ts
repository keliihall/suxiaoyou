import assert from "node:assert/strict";
import { existsSync, readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative } from "node:path";
import test from "node:test";

const first = "open";
const second = "yak";
const compact = `${first}${second}`;
const hyphenated = `${first}-${second}`;
const spaced = `${first} ${second}`;
const oldBrandPattern = new RegExp(`${compact}|${hyphenated}|${spaced}`, "i");
const legalAttributionFiles = new Set([
  "NOTICE",
  "README.md",
  "THIRD_PARTY_NOTICES.md",
  "release-licenses/SOURCE_AVAILABILITY.md",
  "scripts/open-source-readiness.test.mjs",
]);

const repoRoot = join(process.cwd(), "..");
const ignoredDirs = new Set([
  ".git",
  ".next",
  ".pytest_cache",
  ".ruff_cache",
  ".worktrees",
  "__pycache__",
  "build",
  "dist",
  "node_modules",
  "out",
  "target",
  "venv",
]);

const textExtensions = new Set([
  "",
  ".css",
  ".html",
  ".ini",
  ".json",
  ".lock",
  ".md",
  ".mjs",
  ".nsh",
  ".py",
  ".rs",
  ".spec",
  ".svg",
  ".toml",
  ".ts",
  ".tsx",
  ".txt",
  ".yaml",
  ".yml",
]);

function listRepoTextFiles(dir: string): string[] {
  const entries = readdirSync(dir);
  const files: string[] = [];

  for (const entry of entries) {
    const path = join(dir, entry);
    const rel = relative(repoRoot, path);
    const stat = statSync(path);

    if (stat.isDirectory()) {
      if (!ignoredDirs.has(entry)) {
        files.push(...listRepoTextFiles(path));
      }
      continue;
    }

    if (oldBrandPattern.test(rel)) {
      files.push(path);
      continue;
    }

    const ext = entry.includes(".") ? entry.slice(entry.lastIndexOf(".")) : "";
    if (textExtensions.has(ext)) {
      files.push(path);
    }
  }

  return files;
}

test("source and packaging only retain the upstream brand in legal attribution", () => {
  const offenders: string[] = [];

  for (const path of listRepoTextFiles(repoRoot)) {
    const rel = relative(repoRoot, path);
    if (legalAttributionFiles.has(rel)) continue;
    if (oldBrandPattern.test(rel)) {
      offenders.push(rel);
      continue;
    }

    const content = readFileSync(path, "utf8");
    if (oldBrandPattern.test(content)) {
      offenders.push(rel);
    }
  }

  assert.deepEqual(offenders.slice(0, 50), []);
});

test("product identifiers use the 苏小有 namespace", () => {
  const rootPackage = JSON.parse(readFileSync("../package.json", "utf8"));
  const frontendPackage = JSON.parse(readFileSync("package.json", "utf8"));
  const tauriPackage = JSON.parse(readFileSync("../desktop-tauri/package.json", "utf8"));
  const tauriConfig = JSON.parse(readFileSync("../desktop-tauri/src-tauri/tauri.conf.json", "utf8"));
  const cargoToml = readFileSync("../desktop-tauri/src-tauri/Cargo.toml", "utf8");

  assert.equal(rootPackage.name, "suxiaoyou");
  assert.equal(frontendPackage.name, "suxiaoyou-frontend");
  assert.equal(tauriPackage.name, "suxiaoyou-desktop-tauri");
  assert.equal(tauriConfig.identifier, "com.suxiaoyou.desktop");
  assert.match(cargoToml, /name = "suxiaoyou-desktop"/);
  assert.match(cargoToml, /name = "suxiaoyou_desktop_lib"/);
  assert.equal(existsSync("../backend/suxiaoyou.spec"), true);
  assert.equal(existsSync(`../backend/${compact}.spec`), false);
});
