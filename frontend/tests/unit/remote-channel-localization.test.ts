import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const remoteSource = readFileSync("src/app/(main)/remote/content.tsx", "utf8");
const settingsTabs = readFileSync("src/components/settings/settings-tabs.ts", "utf8");
const remotePage = readFileSync("src/app/(main)/remote/page.tsx", "utf8");

test("v0.8.0 does not expose unfinished messaging channel setup", () => {
  for (const hidden of [
    "weixin",
    "feishu",
    "dingtalk",
    "wecom",
    "qq",
    "whatsapp",
    "telegram",
    "discord",
    "slack",
    "email",
    "matrix",
    "mochat",
    "websocket",
  ]) {
    assert.doesNotMatch(remoteSource, new RegExp(`id: "${hidden}"`));
  }
  assert.doesNotMatch(remoteSource, /ChannelsSection|useAddChannel|useRemoveChannel/);
});

test("v0.8.0 hides remote access until the 1.0 service is ready", () => {
  assert.doesNotMatch(settingsTabs, /id: "remote"/);
  assert.match(remotePage, /router\.replace\("\/settings\?tab=general"\)/);
  assert.doesNotMatch(remotePage, /RemoteTabContent/);
});
