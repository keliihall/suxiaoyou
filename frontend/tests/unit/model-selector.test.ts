import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

test("model selection and search rows show names without prices or metadata", () => {
  const selector = readFileSync(
    "src/components/selectors/header-model-dropdown.tsx",
    "utf8",
  );

  assert.match(selector, /value=\{model\.name\}/);
  assert.match(selector, /\{model\.name\}<\/span>/);
  assert.match(selector, /w-\[min\(320px,calc\(100vw-24px\)\)\]/);
  assert.doesNotMatch(selector, /formatUsdPerM|usdToCentsPerM|inputPrice|outputPrice/);
  assert.doesNotMatch(selector, /model\.id !== model\.name|providerLabel|sortBy/);
});

test("model and context controls sit beside send while the header shows the conversation", () => {
  const form = readFileSync("src/components/chat/chat-form.tsx", "utf8");
  const header = readFileSync("src/components/chat/chat-header.tsx", "utf8");

  assert.match(form, /<HeaderModelDropdown compact \/>/);
  assert.match(form, /<ContextIndicator sessionId=\{sessionId\} compact \/>/);
  assert.match(form, /HeaderModelDropdown compact[\s\S]*ContextIndicator[\s\S]*<ChatActions/);
  assert.doesNotMatch(header, /HeaderModelDropdown|ContextIndicator/);
  assert.match(header, /title \|\| t\("common:newChat"\)/);
  assert.match(header, /conversationMenu/);
  assert.match(header, /<MoreHorizontal/);
});
