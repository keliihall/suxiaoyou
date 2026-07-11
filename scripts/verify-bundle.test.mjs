import assert from "node:assert/strict";
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";
import test from "node:test";

const verifyBundleScript = fileURLToPath(
  new URL("./verify-bundle.mjs", import.meta.url),
);
const executableName =
  process.platform === "win32" ? "suxiaoyou-backend.exe" : "suxiaoyou-backend";

// This intentionally mirrors every portable static requirement enforced by
// verify-bundle.mjs. Adding or removing a shipping requirement must update this
// fixture contract explicitly, so a platform-specific filesystem layout cannot
// silently weaken the release gate.
const REQUIRED_STATIC_ASSETS = [
  { kind: "file", relativePath: executableName },
  { kind: "dir", relativePath: "_internal" },
  { kind: "nonempty-dir", relativePath: "_internal/alembic" },
  { kind: "file", relativePath: "_internal/alembic.ini" },
  { kind: "nonempty-dir", relativePath: "_internal/app/agent/prompts" },
  { kind: "file", relativePath: "_internal/app/data/connectors.json" },
  { kind: "dir", relativePath: "_internal/app/data/skills" },
  { kind: "dir", relativePath: "_internal/app/data/plugins" },
  {
    kind: "file",
    relativePath: "_internal/app/data/fonts/SuxiaoyouCJK-Regular.ttf",
  },
  {
    kind: "file",
    relativePath: "_internal/app/data/fonts/OFL-1.1.txt",
  },
  {
    kind: "file",
    relativePath: "_internal/app/data/fonts/PROVENANCE.md",
  },
  { kind: "dir", relativePath: "_internal/frontend_out" },
  { kind: "file", relativePath: "_internal/frontend_out/m.html" },
  { kind: "file", relativePath: "_internal/frontend_out/index.html" },
  {
    kind: "nonempty-dir",
    relativePath: "_internal/frontend_out/_next/static",
  },
  { kind: "nonempty-dir", relativePath: "_internal/uvicorn" },
  { kind: "nonempty-dir", relativePath: "_internal/pydantic_core" },
  { kind: "nonempty-dir", relativePath: "_internal/app" },
];

test("accepts a complete bundle when pure-Python SQLAlchemy only lives in PYZ", (t) => {
  const dist = createCompleteStaticBundle(t);
  const result = verifyBundle(dist);

  assert.equal(result.status, 0, result.stderr);
  assert.match(result.stdout, /static: 18 required assets present/);
  assert.doesNotMatch(result.stderr, /sqlalchemy/i);
});

for (const requirement of REQUIRED_STATIC_ASSETS) {
  test(`still rejects a bundle missing ${requirement.relativePath}`, (t) => {
    const dist = createCompleteStaticBundle(t);
    const target = join(dist, requirement.relativePath);
    rmSync(target, { recursive: true, force: true });

    const result = verifyBundle(dist);

    assert.notEqual(result.status, 0, result.stdout);
    assert.match(result.stderr, new RegExp(escapeRegex(target)));
  });
}

for (const relativePath of [
  "_internal/app/channels/bridge",
  "_internal/app/data/skills_catalog.json",
  "_internal/app/data/skills/doc-coauthoring",
  "_internal/app/data/skills/docx/scripts",
  "_internal/app/data/skills/pdf/scripts",
  "_internal/app/data/skills/pptx/scripts",
  "_internal/app/data/skills/xlsx/scripts",
  "_internal/app/data/skills/canvas-design/design-philosophy-muse.md",
  "_internal/app/data/skills/canvas-design/muse-logo.png",
]) {
  test(`rejects removed or non-redistributable payload ${relativePath}`, (t) => {
    const dist = createCompleteStaticBundle(t);
    const target = join(dist, relativePath);
    mkdirSync(dirname(target), { recursive: true });
    if (relativePath.endsWith(".json") || relativePath.endsWith(".md") || relativePath.endsWith(".png")) {
      writeFileSync(target, "forbidden fixture\n");
    } else {
      mkdirSync(target, { recursive: true });
    }

    const result = verifyBundle(dist);

    assert.notEqual(result.status, 0, result.stdout);
    assert.match(result.stderr, new RegExp(escapeRegex(target)));
  });
}

for (const relativePath of [
  "_internal/_dbm.cpython-312-darwin.so",
  "_internal/adodbapi",
  "_internal/bidi",
  "_internal/svglib-2.0.2.dist-info",
  "_internal/xhtml2pdf",
  "_internal/pyhanko_certvalidator",
]) {
  test(`rejects removed or non-shipping component ${relativePath}`, (t) => {
    const dist = createCompleteStaticBundle(t);
    mkdirSync(join(dist, relativePath), { recursive: true });

    const result = verifyBundle(dist);

    assert.notEqual(result.status, 0, result.stdout);
    assert.match(result.stderr, new RegExp(escapeRegex(join(dist, relativePath))));
  });
}

function createCompleteStaticBundle(t) {
  const dist = mkdtempSync(join(tmpdir(), "verify-bundle-contract-"));
  t.after(() => rmSync(dist, { recursive: true, force: true }));

  for (const requirement of REQUIRED_STATIC_ASSETS) {
    const target = join(dist, requirement.relativePath);
    if (requirement.kind === "file") {
      mkdirSync(dirname(target), { recursive: true });
      writeFileSync(target, "fixture\n");
      continue;
    }

    mkdirSync(target, { recursive: true });
    if (requirement.kind === "nonempty-dir") {
      writeFileSync(join(target, ".fixture"), "fixture\n");
    }
  }

  return dist;
}

function verifyBundle(dist) {
  return spawnSync(process.execPath, [verifyBundleScript, dist], {
    encoding: "utf8",
    env: {
      ...process.env,
      VERIFY_BUNDLE_SKIP_SMOKE: "1",
    },
  });
}

function escapeRegex(value) {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}
