import assert from "node:assert/strict";
import { mkdtempSync, mkdirSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { spawnSync } from "node:child_process";
import test from "node:test";

import {
  ReleaseCheckoutError,
  verifyReleaseCheckout,
} from "./verify-release-checkout.mjs";

function git(root, ...args) {
  const result = spawnSync("git", args, {
    cwd: root,
    encoding: "utf8",
  });
  assert.equal(result.status, 0, result.stderr);
  return result.stdout.trim();
}

function fixture() {
  const root = mkdtempSync(join(tmpdir(), "suxiaoyou-release-checkout-"));
  for (const directory of [
    "backend/app",
    "backend/release_packaging",
    "frontend/src/app",
    "frontend/public",
    "desktop-tauri/src-tauri/src",
    "desktop-tauri/src-tauri/target/release",
    "scripts",
    "release-licenses",
  ]) {
    mkdirSync(join(root, directory), { recursive: true });
  }
  const files = {
    "backend/app/main.py": "APP = 'clean'\n",
    "backend/release_packaging/identity.py": "VALUE = 1\n",
    "backend/run.py": "print('clean')\n",
    "frontend/src/app/page.tsx": "export default function Page() { return null }\n",
    "frontend/public/manifest.json": "{}\n",
    "desktop-tauri/src-tauri/src/main.rs": "fn main() {}\n",
    "scripts/release.mjs": "export {};\n",
    "release-licenses/NOTICE.txt": "notice\n",
    ".gitignore": [
      ".env*",
      "frontend/out/",
      "frontend/public/pdf.worker.min.mjs",
      "frontend/public/cmaps/",
      "frontend/public/standard_fonts/",
      "desktop-tauri/src-tauri/target/",
      "**/__pycache__/",
      "",
    ].join("\n"),
  };
  for (const [path, content] of Object.entries(files)) {
    writeFileSync(join(root, path), content);
  }
  git(root, "init", "--quiet");
  git(root, "config", "user.name", "Release Test");
  git(root, "config", "user.email", "release@example.invalid");
  git(root, "add", ".");
  git(root, "commit", "--quiet", "-m", "fixture");
  return { root, commit: git(root, "rev-parse", "HEAD^{commit}") };
}

function cleanup(root) {
  rmSync(root, { recursive: true, force: true });
}

test("accepts one clean checkout while allowing ignored build outputs", () => {
  const { root, commit } = fixture();
  try {
    mkdirSync(join(root, "frontend/out"), { recursive: true });
    writeFileSync(join(root, "frontend/out/index.html"), "generated\n");
    mkdirSync(join(root, "desktop-tauri/src-tauri/target/release"), { recursive: true });
    writeFileSync(
      join(root, "desktop-tauri/src-tauri/target/release/app"),
      "generated\n",
    );
    const report = verifyReleaseCheckout({ root, expectedRevision: commit });
    assert.equal(report.commit, commit);
  } finally {
    cleanup(root);
  }
});

test("allows only the three reviewed generated PDF.js asset paths", () => {
  const { root, commit } = fixture();
  try {
    writeFileSync(
      join(root, "frontend/public/pdf.worker.min.mjs"),
      "generated worker\n",
    );
    mkdirSync(join(root, "frontend/public/cmaps/nested"), { recursive: true });
    writeFileSync(
      join(root, "frontend/public/cmaps/Adobe-GB1.bcmap"),
      "generated cmap\n",
    );
    writeFileSync(
      join(root, "frontend/public/cmaps/nested/LICENSE"),
      "generated cmap metadata\n",
    );
    mkdirSync(join(root, "frontend/public/standard_fonts"), { recursive: true });
    writeFileSync(
      join(root, "frontend/public/standard_fonts/FoxitSans.pfb"),
      "generated font\n",
    );

    assert.equal(
      verifyReleaseCheckout({ root, expectedRevision: commit }).commit,
      commit,
    );
  } finally {
    cleanup(root);
  }
});

test("rejects every other ignored shadow below frontend/public", () => {
  const { root, commit } = fixture();
  try {
    writeFileSync(
      join(root, ".git/info/exclude"),
      [
        "frontend/public/pdf.worker.min.js",
        "frontend/public/cmaps-shadow/",
        "frontend/public/standard_fonts-shadow/",
        "",
      ].join("\n"),
      { flag: "a" },
    );

    for (const candidate of [
      "frontend/public/pdf.worker.min.js",
      "frontend/public/cmaps-shadow/payload.bcmap",
      "frontend/public/standard_fonts-shadow/payload.pfb",
    ]) {
      const absolute = join(root, candidate);
      mkdirSync(join(absolute, ".."), { recursive: true });
      writeFileSync(absolute, "ignored shadow\n");
      assert.throws(
        () => verifyReleaseCheckout({ root, expectedRevision: commit }),
        /source inventory contains an untracked file/,
      );
      rmSync(absolute);
    }
  } finally {
    cleanup(root);
  }
});

test("rejects HEAD mismatch, tracked drift, and unsafe index flags", () => {
  const first = fixture();
  try {
    assert.throws(
      () => verifyReleaseCheckout({ root: first.root, expectedRevision: "a".repeat(40) }),
      ReleaseCheckoutError,
    );
    writeFileSync(join(first.root, "backend/run.py"), "print('drift')\n");
    assert.throws(
      () => verifyReleaseCheckout({ root: first.root, expectedRevision: first.commit }),
      /tracked files or index are dirty/,
    );
  } finally {
    cleanup(first.root);
  }

  const second = fixture();
  try {
    git(second.root, "update-index", "--assume-unchanged", "backend/run.py");
    assert.throws(
      () => verifyReleaseCheckout({ root: second.root, expectedRevision: second.commit }),
      /unsafe tracked index flags/,
    );
  } finally {
    cleanup(second.root);
  }
});

test("rejects normal and locally ignored source shadows", () => {
  const first = fixture();
  try {
    writeFileSync(join(first.root, "payload.ts"), "malicious\n");
    assert.throws(
      () => verifyReleaseCheckout({ root: first.root, expectedRevision: first.commit }),
      /untracked build input/,
    );
  } finally {
    cleanup(first.root);
  }

  const second = fixture();
  try {
    writeFileSync(
      join(second.root, ".git/info/exclude"),
      "frontend/src/app/payload/\n",
      { flag: "a" },
    );
    mkdirSync(join(second.root, "frontend/src/app/payload"), { recursive: true });
    writeFileSync(
      join(second.root, "frontend/src/app/payload/page.tsx"),
      "export default function Payload() { return null }\n",
    );
    assert.throws(
      () => verifyReleaseCheckout({ root: second.root, expectedRevision: second.commit }),
      /source inventory contains an untracked file/,
    );
  } finally {
    cleanup(second.root);
  }

  const third = fixture();
  try {
    writeFileSync(
      join(third.root, ".git/info/exclude"),
      "frontend/public/payload.svg\ndesktop-tauri/package.json\n",
      { flag: "a" },
    );
    writeFileSync(join(third.root, "frontend/public/payload.svg"), "<svg/>\n");
    assert.throws(
      () => verifyReleaseCheckout({ root: third.root, expectedRevision: third.commit }),
      /source inventory contains an untracked file/,
    );
    rmSync(join(third.root, "frontend/public/payload.svg"));
    writeFileSync(join(third.root, "desktop-tauri/package.json"), "{}\n");
    assert.throws(
      () => verifyReleaseCheckout({ root: third.root, expectedRevision: third.commit }),
      /source inventory contains an untracked file/,
    );
  } finally {
    cleanup(third.root);
  }
});

test("rejects ignored environment inputs but not ignored compiler caches", () => {
  const { root, commit } = fixture();
  try {
    mkdirSync(join(root, "backend/app/__pycache__"), { recursive: true });
    writeFileSync(join(root, "backend/app/__pycache__/main.pyc"), "cache\n");
    assert.equal(
      verifyReleaseCheckout({ root, expectedRevision: commit }).commit,
      commit,
    );
    writeFileSync(join(root, "frontend/.env.production"), "PAYLOAD=1\n");
    assert.throws(
      () => verifyReleaseCheckout({ root, expectedRevision: commit }),
      /ignored environment input/,
    );
  } finally {
    cleanup(root);
  }
});
