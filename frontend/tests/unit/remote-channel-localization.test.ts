import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const remoteSource = readFileSync("src/app/(main)/remote/content.tsx", "utf8");

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
