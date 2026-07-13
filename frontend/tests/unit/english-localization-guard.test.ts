import assert from "node:assert/strict";
import { readdirSync, readFileSync } from "node:fs";
import test from "node:test";

const CJK = /[\u3400-\u9fff\uf900-\ufaff]/u;
const localeRoot = "src/i18n/locales";

function readLocale(language: "en" | "zh", file: string): Record<string, unknown> {
  return JSON.parse(readFileSync(`${localeRoot}/${language}/${file}`, "utf8"));
}

function flattenLocale(
  value: unknown,
  prefix = "",
  leaves: Record<string, unknown> = {},
): Record<string, unknown> {
  if (Array.isArray(value)) {
    value.forEach((item, index) => {
      flattenLocale(item, `${prefix}[${index}]`, leaves);
    });
    if (value.length === 0) leaves[prefix] = value;
    return leaves;
  }
  if (value !== null && typeof value === "object") {
    const entries = Object.entries(value as Record<string, unknown>);
    entries.forEach(([key, child]) => {
      flattenLocale(child, prefix ? `${prefix}.${key}` : key, leaves);
    });
    if (entries.length === 0) leaves[prefix] = value;
    return leaves;
  }
  leaves[prefix] = value;
  return leaves;
}

test("English locale values never contain CJK text", () => {
  for (const file of readdirSync(`${localeRoot}/en`).filter((name) => name.endsWith(".json"))) {
    const values = flattenLocale(readLocale("en", file));
    for (const [key, value] of Object.entries(values)) {
      assert.doesNotMatch(key, CJK, `${file}:${key} key must be fully English`);
      assert.doesNotMatch(String(value), CJK, `${file}:${key} must be fully English`);
    }
  }
});

test("English and Chinese namespaces have matching keys", () => {
  for (const file of readdirSync(`${localeRoot}/en`).filter((name) => name.endsWith(".json"))) {
    assert.deepEqual(
      Object.keys(flattenLocale(readLocale("en", file))).sort(),
      Object.keys(flattenLocale(readLocale("zh", file))).sort(),
      `${file} locale keys must stay in sync`,
    );
  }
});

test("localized display-name contract is explicit", () => {
  const en = readLocale("en", "common.json");
  const zh = readLocale("zh", "common.json");
  assert.equal(en.appName, "suyo");
  assert.equal(zh.appName, "苏小有");
});

test("language-neutral public assets use the English display name", () => {
  for (const file of ["public/manifest.json", "public/favicon.svg", "public/suxiaoyou-logo.svg"]) {
    const source = readFileSync(file, "utf8");
    assert.doesNotMatch(source, CJK, `${file} must be safe to expose in English mode`);
    assert.match(source, /suyo/);
  }
});
