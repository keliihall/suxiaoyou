import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import { getVisibleToolMetadata } from "../../src/lib/tool-metadata.ts";

test("image tool metadata shows provider, model, and a zero catalog estimate", () => {
  const items = getVisibleToolMetadata({
    provider: "siliconflow",
    model: "Kwai-Kolors/Kolors",
    estimated_cost: 0,
    currency: "CNY",
    pricing_unit: "image",
    pricing_as_of: "2026-07-14",
    cost_notice: "Provider bill is authoritative",
  }, "en");

  assert.deepEqual(
    items.map(({ key, value }) => ({ key, value })),
    [
      { key: "provider", value: "SiliconFlow" },
      { key: "model", value: "Kwai-Kolors/Kolors" },
      { key: "estimatedCost", value: "¥0.00/image" },
    ],
  );
  assert.equal(items[2].warning, true);
  assert.match(items[2].title ?? "", /Provider bill is authoritative/);
});

test("actual image cost replaces the estimate when a provider can report it", () => {
  const items = getVisibleToolMetadata({
    provider_name: "Example Images",
    model: "image-v2",
    estimated_cost: 0.1,
    actual_cost: 0.12,
    currency: "CNY",
    pricing_unit: "image",
  }, "zh");

  assert.equal(items.some((item) => item.key === "estimatedCost"), false);
  assert.deepEqual(items.at(-1), {
    key: "actualCost",
    value: "¥0.12/张",
    title: undefined,
  });
});

test("image generation result is a deterministic file card without present_file", () => {
  const messageContent = readFileSync(
    "src/components/messages/message-content.tsx",
    "utf8",
  );
  const messagePresentation = readFileSync(
    "src/lib/message-presentation.ts",
    "utf8",
  );
  const processor = readFileSync("../backend/app/session/processor.py", "utf8");

  assert.match(messagePresentation, /FILE_CARD_TOOL_PARTS[\s\S]*"image_generate"/);
  assert.match(messagePresentation, /GENERATED_FILE_TOOL_PARTS[\s\S]*"image_generate"/);
  assert.match(
    messagePresentation,
    /part\.tool === "image_generate"[\s\S]*typeof metadata\.file_path === "string"/,
  );
  assert.match(messageContent, /fileCardsForTool/);
  assert.match(
    processor,
    /if tool_id == "image_generate" or not metadata:[\s\S]*return ""/,
  );
});

test("stream metadata and paid approval context reach the frontend", () => {
  const streamingTypes = readFileSync("src/types/streaming.ts", "utf8");
  const registry = readFileSync("src/lib/session-stream-registry.ts", "utf8");
  const store = readFileSync("src/stores/chat-store.ts", "utf8");
  const permission = readFileSync(
    "src/components/interactive/permission-dialog.tsx",
    "utf8",
  );
  const zh = JSON.parse(readFileSync("src/i18n/locales/zh/chat.json", "utf8"));
  const en = JSON.parse(readFileSync("src/i18n/locales/en/chat.json", "utf8"));

  assert.match(streamingTypes, /TOOL_METADATA: "tool_metadata"/);
  assert.match(registry, /onCurrent\(SSE_EVENTS\.TOOL_METADATA/);
  assert.match(registry, /setToolMetadata\(/);
  assert.match(store, /metadata: metadata[\s\S]*\.\.\.\(p\.state\.metadata \?\? \{\}\)/);
  assert.match(permission, /data-testid="image-generation-cost-risk"/);
  assert.match(permission, /permission\.metadata\?\.approval_mode === "per_call"/);
  assert.match(permission, /canRememberChoice = !requiresPerCallApproval/);
  assert.match(permission, /canRememberChoice && rememberChoice/);
  assert.match(permission, /<ToolMetadataSummary metadata=\{permission\.metadata\}/);
  assert.match(registry, /workMode === "auto" && !requiresPerCallApproval/);
  assert.match(zh.imageGenerationCostRisk, /最终以供应商账单为准/);
  assert.match(en.imageGenerationApprovalPerCall, /every later generation/);
});
