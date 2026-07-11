import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const remoteConnectionSource = readFileSync("src/lib/remote-connection.ts", "utf8");
const mobileSettingsSource = readFileSync("src/app/(mobile)/m/settings/page.tsx", "utf8");
const mobileLayoutSource = readFileSync("src/app/(mobile)/layout.tsx", "utf8");

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

test("mobile remote provider UI uses localized provider names", () => {
  for (const text of ["深度求索", "通义千问", "月之暗面", "智谱", "硅基流动", "小米"]) {
    assert.match(remoteConnectionSource, new RegExp(text));
  }

  assert.match(mobileSettingsSource, /模型服务商/);
  assert.match(mobileSettingsSource, /远程连接/);
  assert.doesNotMatch(mobileSettingsSource, /ChatGPT Subscription|OpenRouter/);
  assert.doesNotMatch(mobileLayoutSource, /openai-subscription|openrouter/);
});

