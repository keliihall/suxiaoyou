import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { spawnSync } from "node:child_process";
import test from "node:test";

import {
  assertCargoAuditClean,
  renderCargoAuditMarkdown,
  summarizeCargoAudit,
  validateCargoAuditSummary,
} from "./cargo-audit-summary.mjs";

function report({ vulnerabilities = [], warnings = {} } = {}) {
  return {
    vulnerabilities: {
      found: vulnerabilities.length > 0,
      count: vulnerabilities.length,
      list: vulnerabilities,
    },
    warnings,
  };
}

test("derives vulnerability, warning, and dynamic category totals", () => {
  const summary = summarizeCargoAudit(
    report({
      warnings: {
        unsound: [{ id: "one" }],
        notice: [],
        unmaintained: [{ id: "two" }, { id: "three" }],
      },
    }),
  );

  assert.deepEqual(summary.vulnerabilities, { total: 0 });
  assert.deepEqual(summary.warnings, {
    total: 3,
    categories: { notice: 0, unmaintained: 2, unsound: 1 },
  });
  assert.equal(validateCargoAuditSummary(summary), summary);
  assert.equal(assertCargoAuditClean(summary), summary);

  const markdown = renderCargoAuditMarkdown(summary);
  assert.match(markdown, /vulnerabilities 总数：\*\*0\*\*/);
  assert.match(markdown, /warning 总数：\*\*3\*\*/);
  assert.match(markdown, /`notice`：0/);
  assert.match(markdown, /`unmaintained`：2/);
  assert.match(markdown, /`unsound`：1/);
  assert.ok(markdown.endsWith("\n\n"));
});

test("rejects inconsistent cargo-audit input and tampered summaries", () => {
  assert.throws(
    () =>
      summarizeCargoAudit({
        vulnerabilities: { found: false, count: 1, list: [] },
        warnings: {},
      }),
    /count does not match/,
  );
  assert.throws(
    () => summarizeCargoAudit(report({ warnings: { unmaintained: null } })),
    /must be an array/,
  );

  const summary = summarizeCargoAudit(
    report({ warnings: { unmaintained: [{ id: "one" }] } }),
  );
  summary.warnings.total = 2;
  assert.throws(
    () => validateCargoAuditSummary(summary),
    /total does not match category counts/,
  );
});

test("keeps vulnerability findings as a hard release failure", () => {
  const summary = summarizeCargoAudit(
    report({ vulnerabilities: [{ advisory: { id: "RUSTSEC-TEST" } }] }),
  );
  assert.throws(() => assertCargoAuditClean(summary), /reported 1 vulnerabilities/);
});

test("CLI generates, verifies, renders, and fails closed on vulnerabilities", (t) => {
  const directory = mkdtempSync(join(tmpdir(), "suxiaoyou-cargo-audit-"));
  t.after(() => rmSync(directory, { recursive: true, force: true }));
  const rawPath = join(directory, "audit.json");
  const summaryPath = join(directory, "summary.json");
  const markdownPath = join(directory, "summary.md");
  writeFileSync(
    rawPath,
    JSON.stringify(
      report({
        vulnerabilities: [{ advisory: { id: "RUSTSEC-TEST" } }],
        warnings: { yanked: [{ package: "fixture" }] },
      }),
    ),
  );
  const script = join(import.meta.dirname, "cargo-audit-summary.mjs");

  const generate = spawnSync(process.execPath, [script, "generate", rawPath, summaryPath]);
  assert.equal(generate.status, 0, generate.stderr.toString());
  assert.equal(
    spawnSync(process.execPath, [script, "verify", summaryPath]).status,
    0,
  );
  const enforce = spawnSync(process.execPath, [script, "assert-clean", summaryPath]);
  assert.equal(enforce.status, 1);
  assert.match(enforce.stderr.toString(), /reported 1 vulnerabilities/);
  assert.equal(
    spawnSync(process.execPath, [script, "markdown", summaryPath, markdownPath]).status,
    0,
  );
  assert.match(readFileSync(markdownPath, "utf8"), /`yanked`：1/);
});
