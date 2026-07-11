import assert from "node:assert/strict";
import { existsSync, readFileSync, readdirSync } from "node:fs";
import { dirname, join } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const repositoryRoot = join(dirname(fileURLToPath(import.meta.url)), "..");

function read(relativePath) {
  return readFileSync(join(repositoryRoot, relativePath), "utf8");
}

function textFilesUnder(relativeDirectory) {
  const files = [];
  const textExtension = /\.(?:cjs|js|json|jsx|md|mjs|py|ts|tsx)$/i;

  function visit(directory) {
    for (const entry of readdirSync(directory, { withFileTypes: true })) {
      const path = join(directory, entry.name);
      if (entry.isDirectory()) {
        visit(path);
      } else if (entry.isFile() && textExtension.test(entry.name)) {
        files.push(path);
      }
    }
  }

  visit(join(repositoryRoot, relativeDirectory));
  return files;
}

test("repository ships the declared Apache-2.0 license", () => {
  const license = read("LICENSE");
  assert.match(license, /Apache License\s+Version 2\.0, January 2004/);
  assert.match(license, /Copyright 2026 W Axis Inc\./);

  for (const packagePath of [
    "package.json",
    "frontend/package.json",
    "desktop-tauri/package.json",
  ]) {
    assert.equal(JSON.parse(read(packagePath)).license, "Apache-2.0");
  }
  assert.match(
    read("desktop-tauri/src-tauri/Cargo.toml"),
    /^license\s*=\s*"Apache-2\.0"$/m,
  );
});

test("README and NOTICE preserve upstream attribution", () => {
  const readme = read("README.md");
  const notice = read("NOTICE");

  for (const text of [readme, notice]) {
    assert.match(text, /OpenYak/);
    assert.match(text, /https:\/\/github\.com\/openyak\/openyak/);
    assert.match(text, /Apache\s+(?:License\s+)?2\.0/i);
  }
  assert.match(notice, /Copyright 2026 W Axis Inc\./);
});

test("production frontend excludes dependencies without redistribution rights", () => {
  const frontendPackage = JSON.parse(read("frontend/package.json"));
  assert.equal(frontendPackage.dependencies["@kandiforge/pptx-renderer"], undefined);
  assert.doesNotMatch(
    read("frontend/package-lock.json"),
    /@kandiforge\/pptx-renderer|abstractclass\.net/i,
  );
});

test("backend PDF export excludes the LGPL xhtml2pdf dependency chain", () => {
  for (const relativePath of ["backend/pyproject.toml", "backend/requirements.txt"] ) {
    const dependencyFile = read(relativePath);
    assert.doesNotMatch(
      dependencyFile,
      /xhtml2pdf|python-bidi|svglib|pyHanko|pyhanko-certvalidator/i,
    );
  }
});

test("bundled office skills contain only project-owned Apache-2.0 guidance", () => {
  const expectedFiles = {
    docx: ["SKILL.md", "reference.md"],
    pdf: ["SKILL.md", "forms.md", "reference.md"],
    pptx: ["SKILL.md", "reference.md"],
    xlsx: ["SKILL.md", "reference.md"],
  };

  for (const [skillName, files] of Object.entries(expectedFiles)) {
    const directory = join(repositoryRoot, "backend/app/data/skills", skillName);
    assert.deepEqual(readdirSync(directory).sort(), files);
    assert.match(read(`backend/app/data/skills/${skillName}/SKILL.md`), /^license: Apache-2\.0$/m);
  }

  assert.equal(
    existsSync(join(repositoryRoot, "backend/app/data/skills/doc-coauthoring")),
    false,
    "unlicensed upstream doc-coauthoring content must not be bundled",
  );
});

test("release excludes the unreviewed third-party skill catalog", () => {
  const catalogPath = join(
    repositoryRoot,
    "backend/app/data/skills_catalog.json",
  );
  const updaterPath = join(
    repositoryRoot,
    "backend/scripts/update_skills_catalog.py",
  );

  assert.equal(existsSync(catalogPath), false);
  assert.equal(existsSync(updaterPath), false);

  const catalogProviderMarker = ["skills", "mp"].join("");
  const providerPattern = new RegExp(catalogProviderMarker, "i");
  for (const path of [
    ...textFilesUnder("backend/app"),
    ...textFilesUnder("backend/scripts"),
    ...textFilesUnder("frontend/src"),
  ]) {
    assert.doesNotMatch(readFileSync(path, "utf8"), providerPattern, path);
  }

  const skillsApi = read("backend/app/api/skills.py");
  const storeStart = skillsApi.indexOf('@router.get("/skills/store/search")');
  const storeEnd = skillsApi.indexOf('@router.post("/skills/install")');
  assert.ok(storeStart >= 0 && storeEnd > storeStart);
  const storeRoute = skillsApi.slice(storeStart, storeEnd);
  assert.doesNotMatch(
    storeRoute,
    /AsyncClient|httpx|read_text|json\.loads|https?:\/\//i,
  );
  assert.match(storeRoute, /"available": False/);
  assert.match(storeRoute, /"skills": \[\]/);
});

test("public source excludes internal development-session records", () => {
  assert.equal(
    existsSync(join(repositoryRoot, "docs/superpowers")),
    false,
    "internal planning and specification records must not ship in public source",
  );
});

test("runtime data ignore rules do not hide bundled application data", () => {
  const gitignore = read(".gitignore");
  assert.match(gitignore, /^\/data\/$/m);
  assert.match(gitignore, /^\/backend\/data\/$/m);
  assert.doesNotMatch(gitignore, /^data\/$/m);
  assert.doesNotMatch(gitignore, /^(?:build|dist)\*\/$/m);
  assert.match(gitignore, /^\/backend\/build\*\/$/m);
  assert.match(gitignore, /^\/backend\/dist\*\/$/m);
  assert.match(gitignore, /^\/frontend\/out\/$/m);
});

test("public environment example uses current product naming and placeholders", () => {
  const example = read("backend/.env.example");
  assert.doesNotMatch(example, /Muse/i);
  assert.match(example, /SUXIAOYOU_OPENROUTER_API_KEY=sk-or-v1-your-key-here/);
  assert.doesNotMatch(example, /SUXIAOYOU_OPENROUTER_API_KEY=sk-or-v1-[A-Za-z0-9]{20,}/);
});
