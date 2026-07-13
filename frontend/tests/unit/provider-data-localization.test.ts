import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const constants = readFileSync("src/lib/constants.ts", "utf8");
const api = readFileSync("src/lib/api.ts", "utf8");
const autoDetect = readFileSync("src/hooks/use-auto-detect-provider.ts", "utf8");
const providersTab = readFileSync("src/components/settings/providers-tab.tsx", "utf8");
const providerMutations = [
  "src/components/settings/rapid-mlx-panel.tsx",
  "src/components/settings/providers/byok-panel.tsx",
  "src/components/settings/providers/custom-endpoint-panel.tsx",
  "src/components/settings/providers/custom-endpoint-form.tsx",
  "src/components/settings/providers/custom-endpoint-edit-form.tsx",
].map((path) => readFileSync(path, "utf8"));

test("provider cache identity includes the same active language sent to the API", () => {
  assert.match(
    constants,
    /providersForLanguage:\s*\(language: string\)[\s\S]*\["providers",[\s\S]*startsWith\("zh"\) \? "zh" : "en"/,
  );
  assert.match(api, /headers\.set\("Accept-Language",[\s\S]*i18n\.language/);
  assert.match(api, /"Accept-Language": i18n\.language \|\| "en"/);

  for (const source of [autoDetect, providersTab]) {
    assert.match(source, /useTranslation/);
    assert.match(
      source,
      /queryKey: queryKeys\.providersForLanguage\(\s*i18n\.resolvedLanguage \|\| i18n\.language,?\s*\)/,
    );
    assert.doesNotMatch(source, /queryKey: queryKeys\.providers,\s*\n\s*queryFn/);
  }
});

test("provider mutations invalidate the language-neutral provider prefix", () => {
  for (const source of providerMutations) {
    if (!source.includes("queryKeys.providers")) continue;
    assert.match(
      source,
      /invalidateQueries\(\{ queryKey: queryKeys\.providers \}\)/,
    );
  }
});
