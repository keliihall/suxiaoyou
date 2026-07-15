import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

test("plugin and skill toggles reject false and surface persistence errors", () => {
  const hooks = readFileSync("src/hooks/use-plugins.ts", "utf8");
  const en = JSON.parse(readFileSync("src/i18n/locales/en/plugins.json", "utf8"));
  const zh = JSON.parse(readFileSync("src/i18n/locales/zh/plugins.json", "utf8"));

  assert.match(hooks, /if \(!result\.success\) throw new Error/);
  assert.match(hooks, /apiErrorMessage\(error, t\("pluginToggleFailed"\)\)/);
  assert.match(hooks, /apiErrorMessage\(error, t\("skillToggleFailed"\)\)/);
  assert.equal(typeof en.pluginToggleFailed, "string");
  assert.equal(typeof en.skillToggleFailed, "string");
  assert.equal(typeof zh.pluginToggleFailed, "string");
  assert.equal(typeof zh.skillToggleFailed, "string");
});
