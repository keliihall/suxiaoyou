#!/usr/bin/env node

/**
 * Bump the project version across all release metadata.
 *
 * Usage:
 *   node scripts/bump-version.mjs <version>
 *   node scripts/bump-version.mjs patch|minor|major
 */

import fs from "node:fs";
import path from "node:path";

import {
  assertReleaseVersion,
  isMainModule,
  replaceTomlSectionValues,
  verifyReleaseMetadata,
} from "./release-metadata.mjs";
import { syncDesktopMeta } from "./sync-desktop-meta.mjs";

function readJson(filePath) {
  return JSON.parse(fs.readFileSync(filePath, "utf8"));
}

function writeJson(filePath, value) {
  fs.writeFileSync(filePath, `${JSON.stringify(value, null, 2)}\n`);
}

export function updateNpmLockVersion(lock, version, label = "package-lock.json") {
  assertReleaseVersion(version);
  const updated = JSON.parse(JSON.stringify(lock));
  if (!updated || typeof updated !== "object") {
    throw new Error(`${label} must contain a JSON object`);
  }
  if (!updated.packages?.[""] || typeof updated.packages[""] !== "object") {
    throw new Error(`${label} is missing packages[\"\"]`);
  }
  updated.version = version;
  updated.packages[""].version = version;
  return updated;
}

export function updateCargoLockVersion(lock, version) {
  assertReleaseVersion(version);
  let matches = 0;
  const sections = lock.split(/(?=^\[\[package\]\]$)/m);
  const updated = sections.map((section) => {
    if (!/^name = "suxiaoyou-desktop"$/m.test(section)) return section;
    matches += 1;
    if (!/^version = "[^"]+"$/m.test(section)) {
      throw new Error("Cargo.lock suxiaoyou-desktop package is missing version");
    }
    return section.replace(/^version = "[^"]+"$/m, `version = "${version}"`);
  });
  if (matches !== 1) {
    throw new Error(`Cargo.lock must contain exactly one suxiaoyou-desktop package, found ${matches}`);
  }
  return updated.join("");
}

function bump(version, level) {
  const parts = version.split(".").map(Number);
  if (level === "major") return `${parts[0] + 1}.0.0`;
  if (level === "minor") return `${parts[0]}.${parts[1] + 1}.0`;
  if (level === "patch") return `${parts[0]}.${parts[1]}.${parts[2] + 1}`;
  throw new Error(`Unknown bump level: ${level}`);
}

function replaceProjectVersion(rootDir, version) {
  const pyprojectPath = path.join(rootDir, "backend", "pyproject.toml");
  const pyproject = fs.readFileSync(pyprojectPath, "utf8");
  const updated = replaceTomlSectionValues(
    pyproject,
    "project",
    { version },
    "backend/pyproject.toml",
  );
  fs.writeFileSync(pyprojectPath, updated);
}

function updatePoweredBy(rootDir, version) {
  for (const locale of ["en", "zh"]) {
    const commonPath = path.join(
      rootDir,
      "frontend",
      "src",
      "i18n",
      "locales",
      locale,
      "common.json",
    );
    const common = readJson(commonPath);
    common.poweredBy = `苏小有 v${version}`;
    writeJson(commonPath, common);
  }
}

export function resolveTargetVersion(currentVersion, argument) {
  if (!argument) {
    throw new Error("Usage: node scripts/bump-version.mjs <version|patch|minor|major>");
  }
  const version = ["patch", "minor", "major"].includes(argument)
    ? bump(currentVersion, argument)
    : argument;
  assertReleaseVersion(version);
  return version;
}

export function updateProjectVersion(rootDir, version) {
  assertReleaseVersion(version);

  const rootPkgPath = path.join(rootDir, "package.json");
  const rootPkg = readJson(rootPkgPath);
  rootPkg.version = version;
  writeJson(rootPkgPath, rootPkg);
  console.log("  ✓ package.json");

  const frontendPkgPath = path.join(rootDir, "frontend", "package.json");
  const frontendPkg = readJson(frontendPkgPath);
  frontendPkg.version = version;
  writeJson(frontendPkgPath, frontendPkg);
  console.log("  ✓ frontend/package.json");

  replaceProjectVersion(rootDir, version);
  console.log("  ✓ backend/pyproject.toml");

  syncDesktopMeta(rootDir);
  console.log("  ✓ desktop-tauri (tauri.conf.json + Cargo.toml)");

  const rootLockPath = path.join(rootDir, "package-lock.json");
  writeJson(
    rootLockPath,
    updateNpmLockVersion(readJson(rootLockPath), version, "package-lock.json"),
  );
  console.log("  ✓ package-lock.json");

  const frontendLockPath = path.join(rootDir, "frontend", "package-lock.json");
  writeJson(
    frontendLockPath,
    updateNpmLockVersion(
      readJson(frontendLockPath),
      version,
      "frontend/package-lock.json",
    ),
  );
  console.log("  ✓ frontend/package-lock.json");

  const cargoLockPath = path.join(
    rootDir,
    "desktop-tauri",
    "src-tauri",
    "Cargo.lock",
  );
  fs.writeFileSync(
    cargoLockPath,
    updateCargoLockVersion(fs.readFileSync(cargoLockPath, "utf8"), version),
  );
  console.log("  ✓ desktop-tauri/src-tauri/Cargo.lock");

  updatePoweredBy(rootDir, version);
  console.log("  ✓ localized poweredBy strings");

  verifyReleaseMetadata(rootDir, version);
}

function main() {
  try {
    const rootDir = process.cwd();
    const currentVersion = readJson(path.join(rootDir, "package.json")).version;
    const version = resolveTargetVersion(currentVersion, process.argv[2]);

    console.log(`Bumping version: ${currentVersion} → ${version}\n`);
    updateProjectVersion(rootDir, version);
    console.log(`\nDone! All release metadata updated to ${version}.`);
  } catch (error) {
    console.error(error instanceof Error ? error.message : error);
    process.exitCode = 1;
  }
}

if (isMainModule(import.meta.url)) {
  main();
}
