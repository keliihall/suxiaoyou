import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const remoteConnectionSource = readFileSync("src/lib/remote-connection.ts", "utf8");
const mobileSettingsSource = readFileSync("src/app/(mobile)/m/settings/page.tsx", "utf8");
const mobileLayoutSource = readFileSync("src/app/(mobile)/layout.tsx", "utf8");
const enSettings = JSON.parse(readFileSync("src/i18n/locales/en/settings.json", "utf8"));
const zhSettings = JSON.parse(readFileSync("src/i18n/locales/zh/settings.json", "utf8"));

test("remote provider preference only allows China-ready providers", () => {
  for (const provider of [
    "deepseek",
    "qwen",
    "kimi",
    "zhipu",
    "minimax",
    "siliconflow",
    "xiaomi",
  ]) {
    assert.match(remoteConnectionSource, new RegExp(`"${provider}"`));
  }

  assert.doesNotMatch(remoteConnectionSource, /"chatgpt"|"openrouter"/);
});

test("mobile remote provider UI resolves official names through i18n", () => {
  const expectedEnglish = ["DeepSeek", "Qwen", "Kimi", "Zhipu AI", "MiniMax", "SiliconFlow", "Xiaomi MiMo"];
  const providers = ["deepseek", "qwen", "kimi", "zhipu", "minimax", "siliconflow", "xiaomi"];
  providers.forEach((provider, index) => {
    assert.equal(enSettings[`providerName_${provider}`], expectedEnglish[index]);
    assert.match(zhSettings[`providerName_${provider}`], /[\u3400-\u9fff]|Kimi|MiniMax/u);
  });

  assert.match(mobileSettingsSource, /t\(`providerName_\$\{p\.id\}`\)/);
  assert.match(mobileSettingsSource, /t\("mobileModelProvider"\)/);
  assert.match(mobileSettingsSource, /t\("mobileRemoteTitle"\)/);
  assert.doesNotMatch(mobileSettingsSource, /ChatGPT Subscription|OpenRouter/);
  assert.doesNotMatch(mobileLayoutSource, /openai-subscription|openrouter/);
});
