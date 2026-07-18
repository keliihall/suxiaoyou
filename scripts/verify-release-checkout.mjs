#!/usr/bin/env node

/**
 * Fail-closed release source identity check.
 *
 * Generated build outputs are allowed only through reviewed .gitignore rules.
 * Source-bearing roots are also inventoried against Git directly so a local
 * exclude or global ignore cannot hide a module/page/build-script shadow.
 */

import { lstatSync, readdirSync } from "node:fs";
import { dirname, join, relative, resolve, sep } from "node:path";
import { argv, env } from "node:process";
import { spawnSync } from "node:child_process";
import { fileURLToPath, pathToFileURL } from "node:url";

const COMMIT = /^[0-9a-f]{40}$/u;
const repositoryRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const SENSITIVE_ROOTS = Object.freeze([
  "backend/app",
  "backend/alembic",
  "backend/release_packaging",
  "frontend/src",
  "frontend/public",
  "desktop-tauri/src-tauri",
  "scripts",
  "release-licenses",
]);
const SENSITIVE_FILES = Object.freeze([
  "backend/alembic.ini",
  "backend/pyproject.toml",
  "backend/requirements.txt",
  "backend/run.py",
  "backend/suxiaoyou.spec",
  "frontend/next.config.ts",
  "frontend/eslint.config.mjs",
  "frontend/next-env.d.ts",
  "frontend/package-lock.json",
  "frontend/package.json",
  "frontend/postcss.config.mjs",
  "frontend/tsconfig.json",
  "desktop-tauri/package-lock.json",
  "desktop-tauri/package.json",
  "package-lock.json",
  "package.json",
]);
const EXCLUDED_SUBTREES = Object.freeze([
  "desktop-tauri/src-tauri/target",
  "desktop-tauri/src-tauri/gen",
]);
// `npm --prefix frontend ci` materializes these locked pdfjs-dist assets via
// frontend/scripts/prepare-pdfjs-assets.mjs. Keep this allowlist path-exact:
// generated regular files may live at the worker path or below the two asset
// roots, but no sibling in frontend/public (and no generated symlink) is safe.
const REVIEWED_GENERATED_FILES = new Set([
  "frontend/public/pdf.worker.min.mjs",
]);
const REVIEWED_GENERATED_ROOTS = Object.freeze([
  "frontend/public/cmaps",
  "frontend/public/standard_fonts",
]);
const FORBIDDEN_IGNORED_INPUTS = Object.freeze([
  ".env",
  ".env.local",
  ".env.production",
  ".env.production.local",
  "backend/.env",
  "backend/.env.local",
  "frontend/.env",
  "frontend/.env.local",
  "frontend/.env.production",
  "frontend/.env.production.local",
  "desktop-tauri/.env",
  "desktop-tauri/.env.local",
]);

export class ReleaseCheckoutError extends Error {}

export function verifyReleaseCheckout({
  expectedRevision,
  root = repositoryRoot,
} = {}) {
  const repository = resolve(String(root ?? ""));
  if (typeof expectedRevision !== "string" || !COMMIT.test(expectedRevision)) {
    throw new ReleaseCheckoutError("expected release revision is not a full commit");
  }

  const expectedCommit = gitLine(
    repository,
    "rev-parse",
    "--verify",
    `${expectedRevision}^{commit}`,
  );
  const actualCommit = gitLine(
    repository,
    "rev-parse",
    "--verify",
    "HEAD^{commit}",
  );
  if (expectedCommit !== actualCommit) {
    throw new ReleaseCheckoutError("release checkout HEAD does not match the expected commit");
  }

  const trackedEntries = splitNul(git(repository, "ls-files", "-v", "-z"));
  if (
    trackedEntries.length === 0 ||
    trackedEntries.some((entry) => entry.length < 3 || entry[0] !== "H" || entry[1] !== " ")
  ) {
    throw new ReleaseCheckoutError("release checkout has unsafe tracked index flags");
  }
  const trackedPaths = new Set(trackedEntries.map((entry) => entry.slice(2)));

  if (
    git(
      repository,
      "status",
      "--porcelain=v1",
      "-z",
      "--untracked-files=no",
      "--ignore-submodules=none",
    ).length !== 0
  ) {
    throw new ReleaseCheckoutError("release checkout tracked files or index are dirty");
  }

  const untracked = splitNul(
    git(repository, "ls-files", "--others", "--exclude-standard", "-z"),
  );
  if (untracked.length !== 0) {
    throw new ReleaseCheckoutError("release checkout contains an untracked build input");
  }

  for (const candidate of FORBIDDEN_IGNORED_INPUTS) {
    if (safeLstat(join(repository, ...candidate.split("/"))) !== null) {
      throw new ReleaseCheckoutError("release checkout contains an ignored environment input");
    }
  }

  for (const candidate of SENSITIVE_FILES) {
    const info = safeLstat(join(repository, ...candidate.split("/")));
    if (info !== null && !trackedPaths.has(candidate)) {
      throw new ReleaseCheckoutError("release source inventory contains an untracked file");
    }
  }
  for (const rootPath of SENSITIVE_ROOTS) {
    inventorySensitiveRoot(repository, rootPath, trackedPaths);
  }

  return Object.freeze({ commit: actualCommit, trackedFileCount: trackedPaths.size });
}

function inventorySensitiveRoot(repository, rootPath, trackedPaths) {
  const absoluteRoot = join(repository, ...rootPath.split("/"));
  const rootInfo = safeLstat(absoluteRoot);
  if (rootInfo === null) return;
  const pending = [absoluteRoot];
  while (pending.length > 0) {
    const current = pending.pop();
    const relativePath = toPosix(relative(repository, current));
    if (isExcludedSubtree(relativePath)) continue;
    const info = lstatSync(current);
    if (info.isSymbolicLink()) {
      if (!trackedPaths.has(relativePath)) {
        throw new ReleaseCheckoutError("release source inventory contains an untracked link");
      }
      continue;
    }
    if (info.isDirectory()) {
      for (const name of readdirSync(current).sort().reverse()) {
        pending.push(join(current, name));
      }
      continue;
    }
    if (!info.isFile()) {
      throw new ReleaseCheckoutError("release source inventory contains a special file");
    }
    if (
      !trackedPaths.has(relativePath) &&
      !isReviewedGeneratedFile(relativePath)
    ) {
      throw new ReleaseCheckoutError("release source inventory contains an untracked file");
    }
  }
}

function isReviewedGeneratedFile(value) {
  if (REVIEWED_GENERATED_FILES.has(value)) return true;
  return REVIEWED_GENERATED_ROOTS.some((rootPath) =>
    value.startsWith(`${rootPath}/`),
  );
}

function isExcludedSubtree(value) {
  if (value.split("/").includes("__pycache__")) return true;
  return EXCLUDED_SUBTREES.some(
    (rootPath) => value === rootPath || value.startsWith(`${rootPath}/`),
  );
}

function safeLstat(path) {
  try {
    return lstatSync(path);
  } catch (error) {
    if (error && error.code === "ENOENT") return null;
    throw new ReleaseCheckoutError("release source inventory is unavailable");
  }
}

function gitLine(repository, ...args) {
  const output = git(repository, ...args);
  const lines = output.endsWith("\n") ? output.slice(0, -1).split("\n") : output.split("\n");
  if (lines.length !== 1 || !lines[0] || output !== `${lines[0]}\n`) {
    throw new ReleaseCheckoutError("Git returned a non-canonical release identity");
  }
  return lines[0];
}

function git(repository, ...args) {
  const gitEnvironment = Object.fromEntries(
    Object.entries(env).filter(([key]) => !key.startsWith("GIT_")),
  );
  gitEnvironment.GIT_CONFIG_NOSYSTEM = "1";
  gitEnvironment.LC_ALL = "C";
  const result = spawnSync("git", args, {
    cwd: repository,
    encoding: "utf8",
    env: gitEnvironment,
    maxBuffer: 16 * 1024 * 1024,
    timeout: 30_000,
  });
  if (result.status !== 0 || result.error || result.signal || result.stderr) {
    throw new ReleaseCheckoutError("Git release identity check failed");
  }
  return result.stdout;
}

function splitNul(value) {
  if (value === "") return [];
  if (!value.endsWith("\0")) {
    throw new ReleaseCheckoutError("Git returned a non-canonical path inventory");
  }
  return value.slice(0, -1).split("\0");
}

function toPosix(value) {
  return sep === "/" ? value : value.split(sep).join("/");
}

function main() {
  try {
    const report = verifyReleaseCheckout({ expectedRevision: argv[2] });
    console.log(
      `[verify-release-checkout] commit=${report.commit}, tracked=${report.trackedFileCount}`,
    );
  } catch (error) {
    const message = error instanceof Error ? error.message : "release checkout verification failed";
    console.error(`[verify-release-checkout] ERROR: ${message}`);
    process.exitCode = 1;
  }
}

if (import.meta.url === pathToFileURL(resolve(argv[1] ?? "")).href) main();
