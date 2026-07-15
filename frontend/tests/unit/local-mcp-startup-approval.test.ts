import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const componentSource = readFileSync(
  "src/app/(main)/plugins/content.tsx",
  "utf8",
);
const hookSource = readFileSync("src/hooks/use-connectors.ts", "utf8");
const constantsSource = readFileSync("src/lib/constants.ts", "utf8");
const typesSource = readFileSync("src/types/connectors.ts", "utf8");

test("local MCP process approval is an explicit fingerprint-bound action", () => {
  assert.match(typesSource, /"needs_approval"/);
  assert.match(typesSource, /fingerprint: string \| null/);
  assert.match(constantsSource, /approve-local-startup/);
  assert.match(hookSource, /fingerprint,[\s\S]*confirmed: true/);
  assert.match(componentSource, /window\.confirm\(t\("localApprovalPrompt"/);
  assert.match(componentSource, /JSON\.stringify\(approval\.command\)/);
  assert.match(componentSource, /approval\.environment_keys/);
  assert.match(componentSource, /approval\.fingerprint/);
});

test("local MCP approval copy is present in English and Chinese", () => {
  const requiredKeys = [
    "localApprovalRequired",
    "localApprovalUnavailable",
    "localApprovalReview",
    "reviewAndRun",
    "localApprovalPrompt",
  ];
  for (const locale of ["en", "zh"]) {
    const messages = JSON.parse(
      readFileSync(`src/i18n/locales/${locale}/plugins.json`, "utf8"),
    );
    for (const key of requiredKeys) {
      assert.equal(typeof messages[key], "string", `${locale}.${key}`);
      assert.ok(messages[key].trim().length > 0, `${locale}.${key}`);
    }
  }
});
