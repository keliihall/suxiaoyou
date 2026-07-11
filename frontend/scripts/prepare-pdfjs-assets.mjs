import { cpSync, mkdirSync, realpathSync, rmSync, statSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const DEFAULT_FRONTEND_DIR = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "..",
);

const PDFJS_ASSETS = [
  {
    name: "pdf.worker.min.mjs",
    expectedType: "file",
    source: ["node_modules", "pdfjs-dist", "build", "pdf.worker.min.mjs"],
    destination: ["public", "pdf.worker.min.mjs"],
  },
  {
    name: "cmaps",
    expectedType: "directory",
    source: ["node_modules", "pdfjs-dist", "cmaps"],
    destination: ["public", "cmaps"],
  },
  {
    name: "standard_fonts",
    expectedType: "directory",
    source: ["node_modules", "pdfjs-dist", "standard_fonts"],
    destination: ["public", "standard_fonts"],
  },
];

function validateSource(asset) {
  let stats;

  try {
    stats = statSync(asset.sourcePath);
  } catch (error) {
    if (error?.code === "ENOENT" || error?.code === "ENOTDIR") {
      return `${asset.name}: missing ${asset.expectedType} source: ${asset.sourcePath}`;
    }

    return `${asset.name}: unable to inspect ${asset.expectedType} source ${asset.sourcePath}: ${error.message}`;
  }

  const hasExpectedType =
    asset.expectedType === "file" ? stats.isFile() : stats.isDirectory();
  if (hasExpectedType) {
    return null;
  }

  const actualType = stats.isDirectory()
    ? "directory"
    : stats.isFile()
      ? "file"
      : "another filesystem entry";
  return `${asset.name}: expected ${asset.expectedType} source but found ${actualType}: ${asset.sourcePath}`;
}

export function preparePdfjsAssets({
  frontendDir = DEFAULT_FRONTEND_DIR,
  logger = console,
} = {}) {
  const assets = PDFJS_ASSETS.map((asset) => ({
    ...asset,
    sourcePath: path.join(frontendDir, ...asset.source),
    destinationPath: path.join(frontendDir, ...asset.destination),
  }));
  const validationErrors = assets
    .map((asset) => validateSource(asset))
    .filter(Boolean);

  if (validationErrors.length > 0) {
    throw new Error(
      `PDF.js asset validation failed:\n${validationErrors
        .map((message) => `- ${message}`)
        .join("\n")}`,
    );
  }

  const copied = [];

  for (const asset of assets) {
    rmSync(asset.destinationPath, { recursive: true, force: true });
    mkdirSync(path.dirname(asset.destinationPath), { recursive: true });
    cpSync(asset.sourcePath, asset.destinationPath, { recursive: true });
    copied.push(asset.name);
    logger.info(`[pdfjs-assets] Prepared ${asset.name}: ${asset.destinationPath}`);
  }

  return { copied, skipped: [] };
}

function isDirectExecution() {
  if (!process.argv[1]) {
    return false;
  }

  return (
    realpathSync(process.argv[1]) === realpathSync(fileURLToPath(import.meta.url))
  );
}

if (isDirectExecution()) {
  preparePdfjsAssets();
}
