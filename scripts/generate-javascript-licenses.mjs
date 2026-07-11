import { createHash } from "node:crypto";
import { execFileSync } from "node:child_process";
import {
  readFileSync,
  readdirSync,
  writeFileSync,
} from "node:fs";
import { dirname, join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const modulePath = fileURLToPath(import.meta.url);
const repositoryRoot = join(dirname(modulePath), "..");
const frontendRoot = join(repositoryRoot, "frontend");
const outputPath = join(repositoryRoot, "release-licenses", "JAVASCRIPT-LICENSES.txt");

const MIT_PERMISSION = `Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.`;

// Some npm artifacts declare a license but omit the repository's standalone
// license file because of their `files` allowlist. Keep exact, version-scoped
// upstream texts here instead of silently reducing those packages to metadata.
// Sources:
// - https://github.com/theKashey/react-remove-scroll-bar/blob/master/LICENSE
// - https://github.com/juliangruber/isarray/blob/v1.0.0/LICENSE
const CURATED_LICENSE_DOCUMENTS = new Map([
  [
    "isarray@1.0.0",
    `MIT License

Copyright (c) 2013 Julian Gruber <julian@juliangruber.com>

${MIT_PERMISSION}`,
  ],
  [
    "react-remove-scroll-bar@2.3.8",
    `MIT License

Copyright (c) 2025 Anton Korzunov <thekashey@gmail.com>

${MIT_PERMISSION}`,
  ],
]);

// victory-vendor is runtime code used by Recharts. Its npm root omits a
// single LICENSE file but retains exact notices beside all vendored modules.
const RECURSIVE_LICENSE_PACKAGES = new Set(["victory-vendor@37.3.6"]);

export function normalizeRepository(repository) {
  const value = typeof repository === "string" ? repository : repository?.url;
  const normalized = String(value || "")
    .replace(/^git\+/, "")
    .replace(/^git:\/\//, "https://")
    .replace(/\.git$/, "");
  return /^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(normalized)
    ? `https://github.com/${normalized}`
    : normalized;
}

function normalizeAuthor(author) {
  if (typeof author === "string") return author;
  if (!author || typeof author !== "object") return "";
  return [author.name, author.email ? `<${author.email}>` : ""].filter(Boolean).join(" ");
}

export function licenseFiles(packagePath, { recursive = false } = {}) {
  const files = [];

  function visit(directory, depth) {
    for (const entry of readdirSync(directory, { withFileTypes: true })) {
      const path = join(directory, entry.name);
      if (entry.isDirectory()) {
        if (recursive && entry.name !== "node_modules" && depth < 4) {
          visit(path, depth + 1);
        }
        continue;
      }
      if (
        entry.isFile() &&
        /^(?:licen[sc]e|copying|notice|copyright)(?:[._-]|$)/i.test(entry.name) &&
        !/\.(?:cjs|js|jsx|mjs|ts|tsx|map)$/i.test(entry.name)
      ) {
        files.push(relative(packagePath, path));
      }
    }
  }

  try {
    visit(packagePath, 0);
    return files.sort();
  } catch {
    return [];
  }
}

export function curatedLicenseDocument(packageKey) {
  return CURATED_LICENSE_DOCUMENTS.get(packageKey) ?? "";
}

function collectPackages(tree) {
  const packages = new Map();
  function visit(node) {
    if (node.name && node.version && node.path && node.path !== frontendRoot) {
      const key = `${node.name}@${node.version}`;
      if (!packages.has(key)) {
        const manifest = JSON.parse(readFileSync(join(node.path, "package.json"), "utf8"));
        packages.set(key, {
          key,
          path: node.path,
          license: String(manifest.license || node.license || "UNKNOWN"),
          repository: normalizeRepository(manifest.repository || node.repository),
          author: normalizeAuthor(manifest.author || node.author),
        });
      }
    }
    for (const dependency of Object.values(node.dependencies || {})) visit(dependency);
  }
  visit(tree);
  return [...packages.values()].sort((left, right) => left.key.localeCompare(right.key));
}

export function generateJavaScriptLicenses() {
  const tree = JSON.parse(
    execFileSync("npm", ["ls", "--omit=dev", "--all", "--json", "--long"], {
      cwd: frontendRoot,
      encoding: "utf8",
      maxBuffer: 100 * 1024 * 1024,
    }),
  );
  const packages = collectPackages(tree);

  const documents = new Map();
  const fallbackByRepository = new Map();
  for (const pkg of packages) {
    const files = licenseFiles(pkg.path, {
      recursive: RECURSIVE_LICENSE_PACKAGES.has(pkg.key),
    });
    const installedText = files
      .map((name) => readFileSync(join(pkg.path, name), "utf8").replace(/\r\n/g, "\n").trim())
      .filter(Boolean)
      .join("\n\n");
    const text = installedText || curatedLicenseDocument(pkg.key);
    if (!text) continue;
    const digest = createHash("sha256").update(text).digest("hex");
    pkg.document = digest;
    if (!documents.has(digest)) documents.set(digest, { text, packages: [] });
    documents.get(digest).packages.push(pkg);
    if (pkg.repository && !fallbackByRepository.has(pkg.repository)) {
      fallbackByRepository.set(pkg.repository, digest);
    }
  }

  // Monorepos often publish small leaf packages without repeating the root
  // license file. Associate those leaves with an identical-repository license
  // document while retaining their own declared SPDX expression.
  for (const pkg of packages) {
    if (pkg.document || !pkg.repository) continue;
    const digest = fallbackByRepository.get(pkg.repository);
    if (!digest) continue;
    pkg.document = digest;
    documents.get(digest).packages.push(pkg);
  }

  const lines = [
    "JavaScript production dependency licenses",
    "=========================================",
    "",
    "Generated from `frontend/node_modules` with:",
    "`node scripts/generate-javascript-licenses.mjs`.",
    "",
    `Packages in production graph: ${packages.length}`,
    "The application chooses the permissive alternative where a dependency is dual licensed.",
    "",
  ];

  for (const [, document] of [...documents.entries()].sort((left, right) => {
    return left[1].packages[0].key.localeCompare(right[1].packages[0].key);
  })) {
    const group = document.packages.sort((left, right) => left.key.localeCompare(right.key));
    lines.push("-".repeat(78));
    lines.push(`Packages: ${group.map((pkg) => pkg.key).join(", ")}`);
    lines.push(`Declared license(s): ${[...new Set(group.map((pkg) => pkg.license))].join(", ")}`);
    const repositories = [...new Set(group.map((pkg) => pkg.repository).filter(Boolean))];
    if (repositories.length) lines.push(`Source: ${repositories.join(", ")}`);
    const authors = [...new Set(group.map((pkg) => pkg.author).filter(Boolean))];
    if (authors.length) lines.push(`Author/publisher metadata: ${authors.join(", ")}`);
    lines.push("");
    lines.push(document.text);
    lines.push("");
  }

  const missing = packages.filter((pkg) => !pkg.document);
  if (missing.length) {
    lines.push("-".repeat(78));
    lines.push("Packages whose installed npm artifact has no standalone license file");
    lines.push("");
    lines.push(
      "Their package metadata and source locations are preserved below. Standard license texts",
      "also appear in the other grouped entries in this report and in the repository license bundle.",
      "",
    );
    for (const pkg of missing) {
      lines.push(
        `* ${pkg.key} | ${pkg.license} | ${pkg.repository || "source URL absent from package metadata"}`,
      );
    }
    lines.push("");
  }

  writeFileSync(outputPath, `${lines.join("\n")}\n`, "utf8");
  console.log(
    `[javascript-licenses] wrote ${relative(repositoryRoot, outputPath)} for ${packages.length} packages ` +
      `(${documents.size} distinct license documents, ${missing.length} metadata-only)`,
  );
}

if (process.argv[1] && resolve(process.argv[1]) === modulePath) {
  generateJavaScriptLicenses();
}
