#!/usr/bin/env node
import { readdir, readFile, stat, writeFile } from "node:fs/promises";
import { basename, extname, join } from "node:path";

const root = process.argv[2];

if (!root) {
  console.error("Usage: node scripts/sanitize-build-brand.mjs <build-dir>");
  process.exit(1);
}

const textExtensions = new Set([
  "",
  ".css",
  ".html",
  ".js",
  ".json",
  ".map",
  ".mjs",
  ".svg",
  ".txt",
  ".xml",
]);
const extensionlessTextName = /^(?:LICENSE|NOTICE|README)(?:[_-].*)?$/i;
const legalNoticeName = /^(?:(?:LICENSE|NOTICE|COPYING)(?:[._-].*)?|THIRD_PARTY_NOTICES(?:[._-].*)?)$/i;
const utf8Decoder = new TextDecoder("utf-8", { fatal: true });

const first = "open";
const second = "yak";
const compact = `${first}${second}`;
const capitalized = `${first[0].toUpperCase()}${first.slice(1)}${second[0].toUpperCase()}${second.slice(1)}`;
const replacements = [
  [new RegExp(capitalized, "g"), "苏小有"],
  [new RegExp(compact.toUpperCase(), "g"), "SUXIAOYOU"],
  [new RegExp(`${first}-${second}`, "g"), "suxiaoyou"],
  [new RegExp(`${first} ${second}`, "g"), "suxiaoyou"],
  [new RegExp(compact, "g"), "suxiaoyou"],
];

function decodeText(path, buffer) {
  const ext = extname(path).toLowerCase();
  if (!textExtensions.has(ext)) return null;
  if (!ext && !extensionlessTextName.test(basename(path))) return null;
  if (buffer.includes(0)) return null;
  for (const byte of buffer) {
    if ((byte < 0x20 && ![0x09, 0x0a, 0x0c, 0x0d].includes(byte)) || byte === 0x7f) return null;
  }
  try { return utf8Decoder.decode(buffer); } catch { return null; }
}

async function sanitizeFile(path) {
  // Copyright and attribution text must stay byte-for-byte intact. Rebranding
  // a third-party legal notice can misstate ownership or violate its license.
  if (legalNoticeName.test(basename(path))) return;
  const buffer = await readFile(path);
  const original = decodeText(path, buffer);
  if (original === null) return;
  let next = original;
  for (const [pattern, replacement] of replacements) {
    next = next.replace(pattern, replacement);
  }
  if (next !== original) {
    await writeFile(path, next, "utf8");
  }
}

async function walk(dir) {
  const entries = await readdir(dir);
  for (const entry of entries) {
    const path = join(dir, entry);
    const info = await stat(path);
    if (info.isDirectory()) {
      await walk(path);
    } else if (info.isFile()) {
      await sanitizeFile(path);
    }
  }
}

await walk(root);
