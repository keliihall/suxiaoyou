#!/usr/bin/env node

/** Build and validate the compact cargo-audit disclosure used by releases. */

import { readFileSync, writeFileSync } from "node:fs";

import { isMainModule } from "./release-metadata.mjs";

export const CARGO_AUDIT_SUMMARY_SCHEMA_VERSION = 1;
export const CARGO_AUDIT_SUMMARY_KIND = "suxiaoyou-cargo-audit-summary";

function isObject(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function requireObject(value, label) {
  if (!isObject(value)) throw new Error(`${label} must be an object`);
  return value;
}

function requireCount(value, label) {
  if (!Number.isSafeInteger(value) || value < 0) {
    throw new Error(`${label} must be a non-negative integer`);
  }
  return value;
}

function requireExactKeys(value, expected, label) {
  const actual = Object.keys(value).sort();
  const wanted = [...expected].sort();
  if (actual.length !== wanted.length || actual.some((key, index) => key !== wanted[index])) {
    throw new Error(`${label} fields mismatch: expected ${wanted.join(", ")}`);
  }
}

function parseJsonFile(path, label) {
  let parsed;
  try {
    parsed = JSON.parse(readFileSync(path, "utf8"));
  } catch (error) {
    throw new Error(
      `cannot parse ${label} ${path}: ${error instanceof Error ? error.message : String(error)}`,
    );
  }
  return parsed;
}

export function summarizeCargoAudit(report) {
  const root = requireObject(report, "cargo-audit report");
  const vulnerabilities = requireObject(
    root.vulnerabilities,
    "cargo-audit vulnerabilities",
  );
  const vulnerabilityCount = requireCount(
    vulnerabilities.count,
    "cargo-audit vulnerability count",
  );
  if (!Array.isArray(vulnerabilities.list)) {
    throw new Error("cargo-audit vulnerability list must be an array");
  }
  if (vulnerabilities.list.length !== vulnerabilityCount) {
    throw new Error("cargo-audit vulnerability count does not match its finding list");
  }
  if (
    typeof vulnerabilities.found !== "boolean" ||
    vulnerabilities.found !== (vulnerabilityCount > 0)
  ) {
    throw new Error("cargo-audit vulnerability found flag does not match its count");
  }

  const warnings = requireObject(root.warnings, "cargo-audit warnings");
  const categoryEntries = Object.entries(warnings)
    .map(([category, findings]) => {
      if (!/^[a-z0-9][a-z0-9_-]*$/i.test(category)) {
        throw new Error(`cargo-audit warning category is invalid: ${category}`);
      }
      if (!Array.isArray(findings)) {
        throw new Error(`cargo-audit warning category ${category} must be an array`);
      }
      return [category, findings.length];
    })
    .sort(([left], [right]) => left.localeCompare(right));
  const categories = Object.fromEntries(categoryEntries);
  const warningCount = categoryEntries.reduce((total, [, count]) => total + count, 0);

  return {
    schemaVersion: CARGO_AUDIT_SUMMARY_SCHEMA_VERSION,
    kind: CARGO_AUDIT_SUMMARY_KIND,
    tool: "cargo-audit",
    vulnerabilities: { total: vulnerabilityCount },
    warnings: {
      total: warningCount,
      categories,
    },
  };
}

export function validateCargoAuditSummary(summary) {
  const root = requireObject(summary, "cargo-audit summary");
  requireExactKeys(
    root,
    ["schemaVersion", "kind", "tool", "vulnerabilities", "warnings"],
    "cargo-audit summary",
  );
  if (root.schemaVersion !== CARGO_AUDIT_SUMMARY_SCHEMA_VERSION) {
    throw new Error(`unsupported cargo-audit summary schema ${root.schemaVersion}`);
  }
  if (root.kind !== CARGO_AUDIT_SUMMARY_KIND || root.tool !== "cargo-audit") {
    throw new Error("cargo-audit summary identity is invalid");
  }

  const vulnerabilities = requireObject(
    root.vulnerabilities,
    "cargo-audit summary vulnerabilities",
  );
  requireExactKeys(vulnerabilities, ["total"], "cargo-audit summary vulnerabilities");
  requireCount(vulnerabilities.total, "cargo-audit summary vulnerability total");

  const warnings = requireObject(root.warnings, "cargo-audit summary warnings");
  requireExactKeys(warnings, ["total", "categories"], "cargo-audit summary warnings");
  const warningTotal = requireCount(warnings.total, "cargo-audit summary warning total");
  const categories = requireObject(
    warnings.categories,
    "cargo-audit summary warning categories",
  );
  let derivedTotal = 0;
  for (const [category, count] of Object.entries(categories)) {
    if (!/^[a-z0-9][a-z0-9_-]*$/i.test(category)) {
      throw new Error(`cargo-audit summary warning category is invalid: ${category}`);
    }
    derivedTotal += requireCount(
      count,
      `cargo-audit summary warning category ${category}`,
    );
  }
  if (derivedTotal !== warningTotal) {
    throw new Error("cargo-audit summary warning total does not match category counts");
  }
  return root;
}

export function assertCargoAuditClean(summary) {
  const validated = validateCargoAuditSummary(summary);
  if (validated.vulnerabilities.total > 0) {
    throw new Error(
      `cargo-audit reported ${validated.vulnerabilities.total} vulnerabilities`,
    );
  }
  return validated;
}

export function renderCargoAuditMarkdown(summary) {
  const validated = validateCargoAuditSummary(summary);
  const lines = [
    "## Rust 依赖审计",
    "",
    `- \`cargo-audit\` vulnerabilities 总数：**${validated.vulnerabilities.total}**`,
    `- \`cargo-audit\` warning 总数：**${validated.warnings.total}**`,
    "- warning 分类计数：",
  ];
  const categories = Object.entries(validated.warnings.categories).sort(
    ([left], [right]) => left.localeCompare(right),
  );
  if (categories.length === 0) {
    lines.push("  - 无");
  } else {
    for (const [category, count] of categories) {
      lines.push(`  - \`${category}\`：${count}`);
    }
  }
  lines.push("");
  return `${lines.join("\n")}\n`;
}

function usage() {
  return (
    "usage: cargo-audit-summary.mjs " +
    "generate <cargo-audit.json> <summary.json> | " +
    "verify <summary.json> | assert-clean <summary.json> | " +
    "markdown <summary.json> <output.md>"
  );
}

function main() {
  const [command, input, output] = process.argv.slice(2);
  if (!command || !input) throw new Error(usage());

  if (command === "generate") {
    if (!output) throw new Error(usage());
    const summary = summarizeCargoAudit(parseJsonFile(input, "cargo-audit report"));
    validateCargoAuditSummary(summary);
    writeFileSync(output, `${JSON.stringify(summary, null, 2)}\n`);
    console.log(
      `[cargo-audit-summary] wrote ${output}: ` +
        `${summary.vulnerabilities.total} vulnerabilities, ${summary.warnings.total} warnings`,
    );
    return;
  }

  const summary = parseJsonFile(input, "cargo-audit summary");
  if (command === "verify") {
    const validated = validateCargoAuditSummary(summary);
    console.log(
      `[cargo-audit-summary] verified: ${validated.vulnerabilities.total} vulnerabilities, ` +
        `${validated.warnings.total} warnings`,
    );
  } else if (command === "assert-clean") {
    assertCargoAuditClean(summary);
    console.log("[cargo-audit-summary] vulnerability gate passed");
  } else if (command === "markdown") {
    if (!output) throw new Error(usage());
    writeFileSync(output, renderCargoAuditMarkdown(summary));
    console.log(`[cargo-audit-summary] wrote ${output}`);
  } else {
    throw new Error(usage());
  }
}

if (isMainModule(import.meta.url)) {
  try {
    main();
  } catch (error) {
    console.error(
      `[cargo-audit-summary] ${error instanceof Error ? error.message : String(error)}`,
    );
    process.exitCode = 1;
  }
}
