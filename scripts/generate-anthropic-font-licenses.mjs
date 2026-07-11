#!/usr/bin/env node

import { readdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const repositoryRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const fontDirectory = join(
  repositoryRoot,
  "backend/app/data/skills/canvas-design/canvas-fonts",
);
const outputPath = join(
  repositoryRoot,
  "release-licenses/ANTHROPIC-CANVAS-FONTS-OFL-1.1.txt",
);

const licenseFiles = readdirSync(fontDirectory)
  .filter((name) => name.endsWith("-OFL.txt"))
  .sort((left, right) => left.localeCompare(right));

if (licenseFiles.length !== 27) {
  throw new Error(`expected 27 canvas font OFL files, found ${licenseFiles.length}`);
}

const notices = [];
let fullLicense = null;
for (const filename of licenseFiles) {
  const text = readFileSync(join(fontDirectory, filename), "utf8").replaceAll("\r\n", "\n");
  const licenseStart = text.indexOf("-----------------------------------------------------------\nSIL OPEN FONT LICENSE");
  if (licenseStart < 0) {
    throw new Error(`${filename} has no complete SIL Open Font License text`);
  }

  const introduction = text.slice(0, licenseStart).trim();
  const copyrightEnd = introduction.indexOf("\n\nThis Font Software is licensed");
  if (copyrightEnd < 0) {
    throw new Error(`${filename} has no identifiable copyright notice`);
  }
  notices.push(`## ${filename.slice(0, -"-OFL.txt".length)}\n\n${introduction.slice(0, copyrightEnd).trim()}\n`);

  const candidateLicense = text.slice(licenseStart).trim();
  if (fullLicense === null) fullLicense = candidateLicense;
}

const output = [
  "Anthropic Agent Skills canvas font notices",
  "============================================",
  "",
  "These notices cover the font files retained under",
  "backend/app/data/skills/canvas-design/canvas-fonts/.",
  "Source-equivalent Anthropic Agent Skills revision:",
  "7029232b9212482c0476da354b83364bd28fab2f",
  "",
  ...notices,
  "",
  "Complete SIL Open Font License 1.1",
  "==================================",
  "",
  fullLicense,
  "",
].join("\n");

writeFileSync(outputPath, output, "utf8");
console.log(`[generate-anthropic-font-licenses] wrote ${licenseFiles.length} notices to ${outputPath}`);
