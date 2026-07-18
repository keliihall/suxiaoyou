#!/usr/bin/env node

/**
 * Seal generated release inputs between build phases.
 *
 * Source identity is checked separately by verify-release-checkout.mjs. This
 * script covers ignored generated trees (frontend output, frozen backend and
 * bundled Node.js) that are intentionally absent from Git but consumed by a
 * later native packaging phase.
 */

import { createHash } from "node:crypto";
import {
  closeSync,
  constants,
  fstatSync,
  lstatSync,
  openSync,
  readSync,
  readdirSync,
  readlinkSync,
  realpathSync,
  writeFileSync,
  readFileSync,
} from "node:fs";
import { basename, dirname, isAbsolute, join, relative, resolve, sep } from "node:path";
import { argv } from "node:process";
import { fileURLToPath, pathToFileURL } from "node:url";

export const RELEASE_INPUT_SEAL_SCHEMA_VERSION = 1;

const COMMIT = /^(?!0{40}$)[0-9a-f]{40}$/u;
const SHA256 = /^(?!0{64}$)[0-9a-f]{64}$/u;
const repositoryRoot = realpathSync(resolve(dirname(fileURLToPath(import.meta.url)), ".."));

export class ReleaseInputSealError extends Error {}

function canonicalRoot(value, repository) {
  if (
    typeof value !== "string" ||
    value.length === 0 ||
    value.includes("\\") ||
    value.startsWith("/") ||
    value.endsWith("/") ||
    value.split("/").some((part) => !part || part === "." || part === "..")
  ) {
    throw new ReleaseInputSealError("release input root must be a canonical relative path");
  }
  const absolute = resolve(repository, ...value.split("/"));
  const contained = relative(repository, absolute);
  if (!contained || contained === ".." || contained.startsWith(`..${sep}`)) {
    throw new ReleaseInputSealError("release input root escaped the repository");
  }
  const rootInfo = lstatSync(absolute);
  if (!rootInfo.isDirectory() || rootInfo.isSymbolicLink()) {
    throw new ReleaseInputSealError(`release input root is not a real directory: ${value}`);
  }
  return { relative: value, absolute };
}

function addField(hash, value) {
  const bytes = Buffer.from(String(value), "utf8");
  const size = Buffer.allocUnsafe(8);
  size.writeBigUInt64BE(BigInt(bytes.length));
  hash.update(size);
  hash.update(bytes);
}

function sameFileIdentity(left, right) {
  return (
    left.dev === right.dev &&
    left.ino === right.ino &&
    left.mode === right.mode &&
    left.size === right.size &&
    left.mtimeNs === right.mtimeNs &&
    left.ctimeNs === right.ctimeNs
  );
}

function hashStableFile(path, expected) {
  let descriptor = -1;
  try {
    descriptor = openSync(path, constants.O_RDONLY | (constants.O_NOFOLLOW ?? 0));
    const openedBefore = fstatSync(descriptor, { bigint: true });
    if (!openedBefore.isFile() || !sameFileIdentity(expected, openedBefore)) {
      throw new ReleaseInputSealError("release input changed before hashing");
    }
    const digest = createHash("sha256");
    const buffer = Buffer.allocUnsafe(1024 * 1024);
    let size = 0n;
    while (true) {
      const count = readSync(descriptor, buffer, 0, buffer.length, null);
      if (count === 0) break;
      digest.update(buffer.subarray(0, count));
      size += BigInt(count);
    }
    const openedAfter = fstatSync(descriptor, { bigint: true });
    const pathAfter = lstatSync(path, { bigint: true });
    if (
      size !== expected.size ||
      !sameFileIdentity(expected, openedAfter) ||
      !sameFileIdentity(expected, pathAfter)
    ) {
      throw new ReleaseInputSealError("release input changed while hashing");
    }
    return digest.digest("hex");
  } finally {
    if (descriptor >= 0) closeSync(descriptor);
  }
}

function inventoryTree(root) {
  const digest = createHash("sha256");
  const pending = [{ absolute: root.absolute, relative: "." }];
  let entryCount = 0;
  let totalBytes = 0n;
  while (pending.length > 0) {
    const current = pending.pop();
    const before = lstatSync(current.absolute, { bigint: true });
    addField(digest, current.relative);
    addField(digest, Number(before.mode & 0o7777n));
    addField(digest, before.mtimeNs);
    if (before.isDirectory() && !before.isSymbolicLink()) {
      addField(digest, "directory");
      const names = readdirSync(current.absolute).sort();
      const after = lstatSync(current.absolute, { bigint: true });
      if (!sameFileIdentity(before, after)) {
        throw new ReleaseInputSealError("release input directory changed during inventory");
      }
      for (let index = names.length - 1; index >= 0; index -= 1) {
        const name = names[index];
        pending.push({
          absolute: resolve(current.absolute, name),
          relative: current.relative === "." ? name : `${current.relative}/${name}`,
        });
      }
    } else if (before.isFile()) {
      addField(digest, "file");
      addField(digest, before.size);
      addField(digest, hashStableFile(current.absolute, before));
      totalBytes += before.size;
    } else if (before.isSymbolicLink()) {
      addField(digest, "symlink");
      const target = readlinkSync(current.absolute, "utf8");
      const resolvedTarget = resolve(dirname(current.absolute), target);
      const targetRelative = relative(root.absolute, resolvedTarget);
      if (
        !target ||
        isAbsolute(target) ||
        targetRelative === ".." ||
        targetRelative.startsWith(`..${sep}`)
      ) {
        throw new ReleaseInputSealError("release input link escapes its sealed root");
      }
      const after = lstatSync(current.absolute, { bigint: true });
      if (!sameFileIdentity(before, after)) {
        throw new ReleaseInputSealError("release input link changed during inventory");
      }
      addField(digest, target);
    } else {
      throw new ReleaseInputSealError("release input contains a special filesystem object");
    }
    entryCount += 1;
  }
  if (totalBytes > BigInt(Number.MAX_SAFE_INTEGER)) {
    throw new ReleaseInputSealError("release input tree exceeds the supported byte count");
  }
  return Object.freeze({
    path: root.relative,
    entry_count: entryCount,
    total_bytes: Number(totalBytes),
    tree_sha256: digest.digest("hex"),
  });
}

function normalizeRoots(rootPaths, repository) {
  if (!Array.isArray(rootPaths) || rootPaths.length === 0) {
    throw new ReleaseInputSealError("at least one release input root is required");
  }
  if (new Set(rootPaths).size !== rootPaths.length) {
    throw new ReleaseInputSealError("release input roots must be unique");
  }
  return rootPaths.map((path) => canonicalRoot(path, repository));
}

export function createReleaseInputSeal({
  outputPath,
  sourceCommit,
  rootPaths,
  root = repositoryRoot,
} = {}) {
  const commit = String(sourceCommit ?? "").toLowerCase();
  if (!COMMIT.test(commit)) {
    throw new ReleaseInputSealError("source commit must be a full non-zero commit ID");
  }
  const repository = realpathSync(resolve(root));
  const roots = normalizeRoots(rootPaths, repository);
  const report = {
    schema_version: RELEASE_INPUT_SEAL_SCHEMA_VERSION,
    source_commit: commit,
    roots: roots.map(inventoryTree),
  };
  const requestedDestination = resolve(String(outputPath ?? ""));
  const destination = join(realpathSync(dirname(requestedDestination)), basename(requestedDestination));
  if (
    !outputPath ||
    destination === repository ||
    destination.startsWith(`${repository}${sep}`)
  ) {
    throw new ReleaseInputSealError("release input seal must be stored outside the repository");
  }
  try {
    writeFileSync(destination, `${JSON.stringify(report, null, 2)}\n`, {
      encoding: "utf8",
      flag: "wx",
      mode: 0o600,
    });
  } catch (error) {
    throw new ReleaseInputSealError(
      `cannot create release input seal: ${error instanceof Error ? error.message : String(error)}`,
    );
  }
  return Object.freeze(report);
}

function validateStoredSeal(value, expectedCommit, expectedPaths) {
  const seal = value && typeof value === "object" && !Array.isArray(value) ? value : {};
  if (seal.schema_version !== RELEASE_INPUT_SEAL_SCHEMA_VERSION) {
    throw new ReleaseInputSealError("release input seal schema is unsupported");
  }
  if (seal.source_commit !== expectedCommit) {
    throw new ReleaseInputSealError("release input seal commit does not match");
  }
  if (!Array.isArray(seal.roots) || seal.roots.length !== expectedPaths.length) {
    throw new ReleaseInputSealError("release input seal root count does not match");
  }
  for (let index = 0; index < seal.roots.length; index += 1) {
    const stored = seal.roots[index];
    if (
      !stored ||
      typeof stored !== "object" ||
      Array.isArray(stored) ||
      stored.path !== expectedPaths[index] ||
      !Number.isSafeInteger(stored.entry_count) ||
      stored.entry_count <= 0 ||
      !Number.isSafeInteger(stored.total_bytes) ||
      stored.total_bytes < 0 ||
      !SHA256.test(String(stored.tree_sha256 ?? "")) ||
      Object.keys(stored).sort().join(",") !==
        "entry_count,path,total_bytes,tree_sha256"
    ) {
      throw new ReleaseInputSealError("release input seal contains invalid root evidence");
    }
  }
  if (Object.keys(seal).sort().join(",") !== "roots,schema_version,source_commit") {
    throw new ReleaseInputSealError("release input seal contains unexpected fields");
  }
  return seal;
}

export function verifyReleaseInputSeal({
  sealPath,
  sourceCommit,
  rootPaths,
  root = repositoryRoot,
} = {}) {
  const commit = String(sourceCommit ?? "").toLowerCase();
  if (!COMMIT.test(commit)) {
    throw new ReleaseInputSealError("source commit must be a full non-zero commit ID");
  }
  const repository = realpathSync(resolve(root));
  const roots = normalizeRoots(rootPaths, repository);
  let stored;
  try {
    stored = JSON.parse(readFileSync(resolve(String(sealPath ?? "")), "utf8"));
  } catch (error) {
    throw new ReleaseInputSealError(
      `cannot read release input seal: ${error instanceof Error ? error.message : String(error)}`,
    );
  }
  const seal = validateStoredSeal(stored, commit, rootPaths);
  const actual = roots.map(inventoryTree);
  if (JSON.stringify(actual) !== JSON.stringify(seal.roots)) {
    throw new ReleaseInputSealError("generated release input no longer matches its seal");
  }
  return Object.freeze({ source_commit: commit, roots: actual });
}

function main() {
  const [command, sealPath, commit, ...rootPaths] = argv.slice(2);
  try {
    if (command === "create") {
      const report = createReleaseInputSeal({
        outputPath: sealPath,
        sourceCommit: commit,
        rootPaths,
      });
      console.log(`[release-input-seal] created ${report.roots.length} root seal(s)`);
      return;
    }
    if (command === "verify") {
      const report = verifyReleaseInputSeal({
        sealPath,
        sourceCommit: commit,
        rootPaths,
      });
      console.log(`[release-input-seal] verified ${report.roots.length} root seal(s)`);
      return;
    }
    throw new ReleaseInputSealError("usage: release-input-seal.mjs create|verify <seal> <commit> <root...>");
  } catch (error) {
    console.error(
      `[release-input-seal] ERROR: ${error instanceof Error ? error.message : String(error)}`,
    );
    process.exitCode = 1;
  }
}

if (import.meta.url === pathToFileURL(resolve(argv[1] ?? "")).href) main();
