import assert from "node:assert/strict";
import {
  chmodSync,
  mkdtempSync,
  mkdirSync,
  rmSync,
  symlinkSync,
  utimesSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import test from "node:test";

import {
  ReleaseInputSealError,
  createReleaseInputSeal,
  verifyReleaseInputSeal,
} from "./release-input-seal.mjs";

const COMMIT = "a".repeat(40);

function fixture() {
  const parent = mkdtempSync(join(tmpdir(), "suxiaoyou-release-input-"));
  const root = join(parent, "repository");
  const sealPath = join(parent, "seal.json");
  mkdirSync(join(root, "frontend/out/assets"), { recursive: true });
  mkdirSync(join(root, "backend/dist"), { recursive: true });
  writeFileSync(join(root, "frontend/out/index.html"), "clean\n");
  writeFileSync(join(root, "frontend/out/assets/app.js"), "export {};\n");
  writeFileSync(join(root, "backend/dist/backend.bin"), "binary\n");
  return { parent, root, sealPath };
}

function cleanup(parent) {
  rmSync(parent, { recursive: true, force: true });
}

test("creates and verifies an exact multi-root generated-input seal", () => {
  const value = fixture();
  try {
    const created = createReleaseInputSeal({
      outputPath: value.sealPath,
      sourceCommit: COMMIT,
      rootPaths: ["frontend/out", "backend/dist"],
      root: value.root,
    });
    assert.equal(created.source_commit, COMMIT);
    assert.equal(created.roots.length, 2);
    const verified = verifyReleaseInputSeal({
      sealPath: value.sealPath,
      sourceCommit: COMMIT,
      rootPaths: ["frontend/out", "backend/dist"],
      root: value.root,
    });
    assert.deepEqual(verified.roots, created.roots);
  } finally {
    cleanup(value.parent);
  }
});

test("rejects content, size, mode, link, extra-entry, commit, and root drift", () => {
  for (const mutate of [
    ({ root }) => writeFileSync(join(root, "frontend/out/index.html"), "dirty\n"),
    ({ root }) => writeFileSync(join(root, "frontend/out/index.html"), "clean plus\n"),
    ({ root }) => chmodSync(join(root, "frontend/out/index.html"), 0o744),
    ({ root }) =>
      utimesSync(join(root, "frontend/out/index.html"), new Date(1_000), new Date(1_000)),
    ({ root }) => writeFileSync(join(root, "frontend/out/extra.html"), "extra\n"),
    ({ root }) => symlinkSync("index.html", join(root, "frontend/out/current")),
  ]) {
    const value = fixture();
    try {
      createReleaseInputSeal({
        outputPath: value.sealPath,
        sourceCommit: COMMIT,
        rootPaths: ["frontend/out"],
        root: value.root,
      });
      mutate(value);
      assert.throws(
        () =>
          verifyReleaseInputSeal({
            sealPath: value.sealPath,
            sourceCommit: COMMIT,
            rootPaths: ["frontend/out"],
            root: value.root,
          }),
        ReleaseInputSealError,
      );
    } finally {
      cleanup(value.parent);
    }
  }

  const value = fixture();
  try {
    createReleaseInputSeal({
      outputPath: value.sealPath,
      sourceCommit: COMMIT,
      rootPaths: ["frontend/out"],
      root: value.root,
    });
    assert.throws(
      () =>
        verifyReleaseInputSeal({
          sealPath: value.sealPath,
          sourceCommit: "b".repeat(40),
          rootPaths: ["frontend/out"],
          root: value.root,
        }),
      /commit does not match/,
    );
    assert.throws(
      () =>
        verifyReleaseInputSeal({
          sealPath: value.sealPath,
          sourceCommit: COMMIT,
          rootPaths: ["backend/dist"],
          root: value.root,
        }),
      /invalid root evidence/,
    );
  } finally {
    cleanup(value.parent);
  }
});

test("rejects duplicate or escaping roots and refuses a repository-local seal", () => {
  const value = fixture();
  try {
    assert.throws(
      () =>
        createReleaseInputSeal({
          outputPath: value.sealPath,
          sourceCommit: COMMIT,
          rootPaths: ["frontend/out", "frontend/out"],
          root: value.root,
        }),
      /must be unique/,
    );
    assert.throws(
      () =>
        createReleaseInputSeal({
          outputPath: value.sealPath,
          sourceCommit: COMMIT,
          rootPaths: ["../outside"],
          root: value.root,
        }),
      /canonical relative path/,
    );
    assert.throws(
      () =>
        createReleaseInputSeal({
          outputPath: join(value.root, "seal.json"),
          sourceCommit: COMMIT,
          rootPaths: ["frontend/out"],
          root: value.root,
        }),
      /outside the repository/,
    );
  } finally {
    cleanup(value.parent);
  }
});

test("accepts only relative symlinks whose targets remain inside the sealed root", () => {
  const accepted = fixture();
  try {
    symlinkSync("index.html", join(accepted.root, "frontend/out/current"));
    createReleaseInputSeal({
      outputPath: accepted.sealPath,
      sourceCommit: COMMIT,
      rootPaths: ["frontend/out"],
      root: accepted.root,
    });
    assert.doesNotThrow(() =>
      verifyReleaseInputSeal({
        sealPath: accepted.sealPath,
        sourceCommit: COMMIT,
        rootPaths: ["frontend/out"],
        root: accepted.root,
      }),
    );
  } finally {
    cleanup(accepted.parent);
  }

  const rejected = fixture();
  try {
    symlinkSync("../../../outside", join(rejected.root, "frontend/out/escape"));
    assert.throws(
      () =>
        createReleaseInputSeal({
          outputPath: rejected.sealPath,
          sourceCommit: COMMIT,
          rootPaths: ["frontend/out"],
          root: rejected.root,
        }),
      /link escapes/,
    );
  } finally {
    cleanup(rejected.parent);
  }
});
