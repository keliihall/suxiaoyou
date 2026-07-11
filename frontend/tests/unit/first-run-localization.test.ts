import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const onboardingSource = readFileSync("src/components/onboarding/onboarding-screen.tsx", "utf8");
const splashSource = readFileSync("src/components/layout/splash-screen.tsx", "utf8");

test("first-run onboarding uses fully localized Chinese copy", () => {
  const forbiddenEnglish = [
    "Welcome to",
    "Your local-first agent workbench",
    "Start with a local endpoint",
    "Set Up Provider",
    "Continue",
  ];

  for (const phrase of forbiddenEnglish) {
    assert.doesNotMatch(onboardingSource, new RegExp(phrase.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")));
  }

  for (const phrase of ["欢迎使用苏小有", "本地优先的桌面 AI 助理", "配置服务商", "直接开始"]) {
    assert.match(onboardingSource, new RegExp(phrase.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")));
  }
});

test("startup splash tagline is localized", () => {
  assert.doesNotMatch(splashSource, /Local-first desktop agent/);
  assert.match(splashSource, /本地优先的桌面 AI 助理/);
});
