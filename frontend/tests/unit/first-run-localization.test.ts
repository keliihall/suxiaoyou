import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const onboardingSource = readFileSync("src/components/onboarding/onboarding-screen.tsx", "utf8");
const splashSource = readFileSync("src/components/layout/splash-screen.tsx", "utf8");
const enCommon = JSON.parse(readFileSync("src/i18n/locales/en/common.json", "utf8"));
const zhCommon = JSON.parse(readFileSync("src/i18n/locales/zh/common.json", "utf8"));

test("first-run onboarding renders copy through i18n", () => {
  for (const key of [
    "onboardingWelcome",
    "onboardingDescription",
    "onboardingConfigureProvider",
    "onboardingStartNow",
  ]) {
    assert.match(onboardingSource, new RegExp(`t\\(\"${key}\"\\)`));
  }

  assert.equal(enCommon.onboardingWelcome, "Welcome to suyo");
  assert.equal(zhCommon.onboardingWelcome, "欢迎使用苏小有");
  assert.doesNotMatch(enCommon.onboardingDescription, /[\u3400-\u9fff]/u);
});

test("startup splash name and tagline follow the selected language", () => {
  assert.match(splashSource, /t\("appName"\)/);
  assert.match(splashSource, /t\("localFirstTagline"\)/);
  assert.equal(enCommon.appName, "suyo");
  assert.equal(zhCommon.appName, "苏小有");
});
