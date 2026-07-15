import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  DESKTOP_PERMISSION_SOURCE,
  migrateSavedPermissionRules,
  savedPermissionRulesForContext,
  upsertSavedPermissionRule,
  type SavedPermissionRule,
} from "../../src/lib/saved-permissions.ts";

const workspace = "/Users/alex/suyo-workspace";

function exactRule(overrides: Partial<SavedPermissionRule> = {}): SavedPermissionRule {
  return {
    tool: "bash",
    allow: true,
    pattern: "npm run test:ui",
    workspace,
    sessionId: null,
    source: DESKTOP_PERMISSION_SOURCE,
    timestamp: 100,
    ...overrides,
  };
}

test("v4 tool-only rules are removed instead of migrated to wildcard access", () => {
  assert.deepEqual(
    migrateSavedPermissionRules(
      [{ tool: "bash", allow: true, timestamp: 100 }],
      workspace,
    ),
    [],
  );
});

test("an explicit legacy pattern is retained and narrowed to the current workspace", () => {
  assert.deepEqual(
    migrateSavedPermissionRules(
      [{
        tool: "write",
        allow: false,
        pattern: "reports/q3.docx",
        timestamp: 200,
      }],
      workspace,
    ),
    [{
      tool: "write",
      allow: false,
      pattern: "reports/q3.docx",
      workspace,
      sessionId: null,
      source: DESKTOP_PERMISSION_SOURCE,
      timestamp: 200,
    }],
  );
});

test("remembered rules preserve exact patterns and do not cross workspaces", () => {
  const rules = [exactRule()];
  const matching = savedPermissionRulesForContext(rules, {
    workspace,
    sessionId: "session-a",
    source: DESKTOP_PERMISSION_SOURCE,
  });
  assert.deepEqual(matching, [{
    action: "allow",
    permission: "bash",
    pattern: "npm run test:ui",
  }]);
  assert.equal(matching.some((rule) => rule.pattern === "*"), false);

  assert.deepEqual(
    savedPermissionRulesForContext(rules, {
      workspace: "/Users/alex/other-workspace",
      sessionId: "session-a",
      source: DESKTOP_PERMISSION_SOURCE,
    }),
    [],
  );
});

test("folderless remembered rules are confined to one conversation", () => {
  const rules = [exactRule({ workspace: null, sessionId: "session-a" })];
  assert.equal(
    savedPermissionRulesForContext(rules, {
      workspace: null,
      sessionId: "session-a",
      source: DESKTOP_PERMISSION_SOURCE,
    }).length,
    1,
  );
  assert.deepEqual(
    savedPermissionRulesForContext(rules, {
      workspace: null,
      sessionId: "session-b",
      source: DESKTOP_PERMISSION_SOURCE,
    }),
    [],
  );
});

test("a narrower conversation decision overrides the same workspace decision", () => {
  const rules = [
    exactRule({ allow: true }),
    exactRule({ allow: false, workspace: null, sessionId: "session-a" }),
  ];
  assert.deepEqual(
    savedPermissionRulesForContext(rules, {
      workspace,
      sessionId: "session-a",
      source: DESKTOP_PERMISSION_SOURCE,
    }),
    [{
      action: "deny",
      permission: "bash",
      pattern: "npm run test:ui",
    }],
  );
});

test("multiple exact patterns for one tool coexist and only the same scope is replaced", () => {
  const first = upsertSavedPermissionRule([], {
    tool: "bash",
    allow: true,
    pattern: "npm run test:ui",
    workspace,
    sessionId: null,
    source: DESKTOP_PERMISSION_SOURCE,
  }, 100);
  const second = upsertSavedPermissionRule(first, {
    tool: "bash",
    allow: false,
    pattern: "npm run lint",
    workspace,
    sessionId: null,
    source: DESKTOP_PERMISSION_SOURCE,
  }, 200);
  const replaced = upsertSavedPermissionRule(second, {
    tool: "bash",
    allow: false,
    pattern: "npm run test:ui",
    workspace,
    sessionId: null,
    source: DESKTOP_PERMISSION_SOURCE,
  }, 300);

  assert.equal(replaced.length, 2);
  assert.deepEqual(
    replaced.map(({ pattern, allow }) => ({ pattern, allow })),
    [
      { pattern: "npm run lint", allow: false },
      { pattern: "npm run test:ui", allow: false },
    ],
  );
});

test("every chat submission path uses scoped remembered rules", () => {
  const source = readFileSync("src/hooks/use-chat.ts", "utf8");
  assert.equal(
    source.match(/rememberedPermissionRules\(/g)?.length,
    6,
    "one helper definition plus five submission paths, including Goal start",
  );
  assert.doesNotMatch(
    source,
    /savedPermissions\.map\([\s\S]{0,240}pattern:\s*["']\*["']/,
  );
  assert.match(source, /pattern:\s*explicitPattern/);
  assert.match(source, /remember:\s*shouldRemember/);
});
