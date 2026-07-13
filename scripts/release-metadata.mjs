#!/usr/bin/env node

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const RELEASE_VERSION_PATTERN = /^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$/;

function readJson(rootDir, relativePath) {
  return JSON.parse(fs.readFileSync(path.join(rootDir, relativePath), "utf8"));
}

function readText(rootDir, relativePath) {
  return fs.readFileSync(path.join(rootDir, relativePath), "utf8");
}

function escapeRegExp(value) {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function findTomlSection(text, section, source) {
  const headerPattern = new RegExp(
    `^[ \\t]*\\[${escapeRegExp(section)}\\][ \\t]*(?:#.*)?$`,
    "gm",
  );
  const sectionMatches = [...text.matchAll(headerPattern)];
  if (sectionMatches.length === 0) {
    throw new Error(`${source} is missing [${section}]`);
  }
  if (sectionMatches.length > 1) {
    throw new Error(`${source} contains multiple [${section}] sections`);
  }

  const sectionMatch = sectionMatches[0];
  const sectionStart = sectionMatch.index + sectionMatch[0].length;
  const remainder = text.slice(sectionStart);
  const nextSection = remainder.search(
    /^[ \t]*\[\[?[^\]\r\n]+\]\]?[ \t]*(?:#.*)?$/m,
  );
  const sectionEnd = nextSection === -1 ? text.length : sectionStart + nextSection;
  return { sectionStart, sectionEnd };
}

function tomlStringKeyPattern(key) {
  return new RegExp(
    `^([ \\t]*${escapeRegExp(key)}[ \\t]*=[ \\t]*)"([^"\\r\\n]*)"(.*)$`,
    "gm",
  );
}

function readTomlSectionValue(text, section, key, source) {
  const { sectionStart, sectionEnd } = findTomlSection(text, section, source);
  const sectionText = text.slice(sectionStart, sectionEnd);
  const valueMatches = [...sectionText.matchAll(tomlStringKeyPattern(key))];
  if (valueMatches.length === 0) {
    throw new Error(`${source} is missing [${section}].${key}`);
  }
  if (valueMatches.length > 1) {
    throw new Error(`${source} contains multiple [${section}].${key} values`);
  }

  return valueMatches[0][2];
}

export function replaceTomlSectionValues(text, section, updates, source) {
  const { sectionStart, sectionEnd } = findTomlSection(text, section, source);
  let sectionText = text.slice(sectionStart, sectionEnd);

  for (const [key, value] of Object.entries(updates)) {
    const keyPattern = tomlStringKeyPattern(key);
    const keyMatches = [...sectionText.matchAll(keyPattern)];
    if (keyMatches.length === 0) {
      throw new Error(`${source} is missing [${section}].${key}`);
    }
    if (keyMatches.length > 1) {
      throw new Error(`${source} contains multiple [${section}].${key} values`);
    }
    sectionText = sectionText.replace(
      keyPattern,
      (_match, prefix, _currentValue, suffix) => `${prefix}${JSON.stringify(value)}${suffix}`,
    );
  }

  return `${text.slice(0, sectionStart)}${sectionText}${text.slice(sectionEnd)}`;
}

function readCargoLockPackageVersion(text, packageName, source) {
  for (const block of text.split(/(?=^\[\[package\]\]\s*$)/m)) {
    const name = /^name\s*=\s*"([^"]+)"/m.exec(block)?.[1];
    if (name !== packageName) continue;

    const version = /^version\s*=\s*"([^"]+)"/m.exec(block)?.[1];
    if (!version) {
      throw new Error(`${source} package ${packageName} is missing version`);
    }
    return version;
  }

  throw new Error(`${source} is missing package ${packageName}`);
}

function readUniqueEmbeddedVersion(text, pattern, source) {
  const matches = [...text.matchAll(pattern)];
  if (matches.length === 0) {
    throw new Error(`${source} is missing its embedded release version`);
  }
  if (matches.length > 1) {
    throw new Error(`${source} contains multiple embedded release versions`);
  }
  return matches[0][1];
}

export function assertReleaseVersion(version) {
  if (!RELEASE_VERSION_PATTERN.test(version ?? "")) {
    throw new Error(`Invalid expected version "${version ?? ""}". Expected format: X.Y.Z`);
  }
}

export function isMainModule(metaUrl, argvPath = process.argv[1]) {
  if (!argvPath) return false;
  try {
    return fs.realpathSync(fileURLToPath(metaUrl)) === fs.realpathSync(argvPath);
  } catch {
    return false;
  }
}

export function collectReleaseMetadata(rootDir) {
  const checks = [
    {
      source: "package.json",
      read: () => readJson(rootDir, "package.json").version,
      kind: "version",
    },
    {
      source: "package-lock.json top-level version",
      read: () => readJson(rootDir, "package-lock.json").version,
      kind: "version",
    },
    {
      source: "package-lock.json root entry",
      read: () => readJson(rootDir, "package-lock.json").packages?.[""]?.version,
      kind: "version",
    },
    {
      source: "frontend/package.json",
      read: () => readJson(rootDir, "frontend/package.json").version,
      kind: "version",
    },
    {
      source: "frontend/package-lock.json top-level version",
      read: () => readJson(rootDir, "frontend/package-lock.json").version,
      kind: "version",
    },
    {
      source: "frontend/package-lock.json root entry",
      read: () => readJson(rootDir, "frontend/package-lock.json").packages?.[""]?.version,
      kind: "version",
    },
    {
      source: "backend/pyproject.toml [project].version",
      read: () =>
        readTomlSectionValue(
          readText(rootDir, "backend/pyproject.toml"),
          "project",
          "version",
          "backend/pyproject.toml",
        ),
      kind: "version",
    },
    {
      source: "desktop-tauri/src-tauri/tauri.conf.json",
      read: () => readJson(rootDir, "desktop-tauri/src-tauri/tauri.conf.json").version,
      kind: "version",
    },
    {
      source: "desktop-tauri/src-tauri/Cargo.toml [package].version",
      read: () =>
        readTomlSectionValue(
          readText(rootDir, "desktop-tauri/src-tauri/Cargo.toml"),
          "package",
          "version",
          "desktop-tauri/src-tauri/Cargo.toml",
        ),
      kind: "version",
    },
    {
      source: "desktop-tauri/src-tauri/Cargo.lock suxiaoyou-desktop",
      read: () =>
        readCargoLockPackageVersion(
          readText(rootDir, "desktop-tauri/src-tauri/Cargo.lock"),
          "suxiaoyou-desktop",
          "desktop-tauri/src-tauri/Cargo.lock",
        ),
      kind: "version",
    },
    {
      source: "frontend/src/i18n/locales/en/common.json poweredBy",
      read: () => readJson(rootDir, "frontend/src/i18n/locales/en/common.json").poweredBy,
      kind: "poweredByEn",
    },
    {
      source: "frontend/src/i18n/locales/zh/common.json poweredBy",
      read: () => readJson(rootDir, "frontend/src/i18n/locales/zh/common.json").poweredBy,
      kind: "poweredByZh",
    },
    {
      source: "THIRD_PARTY_NOTICES.md release graph",
      read: () =>
        readUniqueEmbeddedVersion(
          readText(rootDir, "THIRD_PARTY_NOTICES.md"),
          /\bv(\d+\.\d+\.\d+) production graphs\b/g,
          "THIRD_PARTY_NOTICES.md",
        ),
      kind: "version",
    },
    {
      source: "release-licenses/SOURCE_AVAILABILITY.md release",
      read: () =>
        readUniqueEmbeddedVersion(
          readText(rootDir, "release-licenses/SOURCE_AVAILABILITY.md"),
          /MPL-2\.0 components included in 苏小有 v(\d+\.\d+\.\d+)\./g,
          "release-licenses/SOURCE_AVAILABILITY.md",
        ),
      kind: "version",
    },
    {
      source: "release-licenses/RUST-LICENSES.html desktop crate",
      read: () =>
        readUniqueEmbeddedVersion(
          readText(rootDir, "release-licenses/RUST-LICENSES.html"),
          />suxiaoyou-desktop (\d+\.\d+\.\d+)<\/a>/g,
          "release-licenses/RUST-LICENSES.html",
        ),
      kind: "version",
    },
  ];

  return checks.map(({ source, read, kind }) => {
    try {
      return { source, value: read(), kind };
    } catch (error) {
      return {
        source,
        error: error instanceof Error ? error.message : String(error),
        kind,
      };
    }
  });
}

export function verifyReleaseMetadata(rootDir, expectedVersion) {
  assertReleaseVersion(expectedVersion);

  const mismatches = collectReleaseMetadata(rootDir).flatMap(({ source, value, error, kind }) => {
    const expected =
      kind === "poweredByEn"
        ? `suyo v${expectedVersion}`
        : kind === "poweredByZh"
          ? `苏小有 v${expectedVersion}`
          : expectedVersion;
    if (error) return [{ source, expected, error }];
    return value === expected ? [] : [{ source, expected, value }];
  });

  if (mismatches.length > 0) {
    const details = mismatches
      .map(
        ({ source, expected, value, error }) =>
          error
            ? `- ${source}: could not read value (${error})`
            : `- ${source}: expected ${JSON.stringify(expected)}, found ${JSON.stringify(value)}`,
      )
      .join("\n");
    throw new Error(`Release metadata does not match ${expectedVersion}:\n${details}`);
  }

  return expectedVersion;
}

function main() {
  const expectedVersion = process.argv[2];
  try {
    verifyReleaseMetadata(process.cwd(), expectedVersion);
    console.log(`Release metadata verified at ${expectedVersion}`);
  } catch (error) {
    console.error(error instanceof Error ? error.message : error);
    process.exitCode = 1;
  }
}

if (isMainModule(import.meta.url)) {
  main();
}
