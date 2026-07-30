import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

test("stream failures never promote raw backend diagnostics to user messages", () => {
  const registry = readFileSync("src/lib/session-stream-registry.ts", "utf8");
  const handler = registry.slice(
    registry.indexOf("const handleAgentError"),
    registry.indexOf("onCurrent(SSE_EVENTS.AGENT_ERROR"),
  );

  assert.match(handler, /const rawMessage = data\.error_message \?\? "";/);
  assert.match(handler, /i18n\.t\("streamErrorGeneric", \{ ns: "chat" \}\)/);
  assert.doesNotMatch(handler, /toast\.error\(rawMessage/);
  assert.doesNotMatch(handler, /:\s*data\.error_message/);
});

test("shared chat operations use translation keys instead of exception messages", () => {
  const chat = readFileSync("src/hooks/use-chat.ts", "utf8");

  assert.match(chat, /i18n\.t\("messageSendFailed", \{ ns: "chat" \}\)/);
  assert.match(chat, /i18n\.t\("taskBatchStartFailed", \{ ns: "chat" \}\)/);
  assert.match(chat, /i18n\.t\("conversationEditFailed", \{ ns: "chat" \}\)/);
  assert.doesNotMatch(chat, /toast\.error\((err|error)\.message/);
  assert.doesNotMatch(chat, /detail\?\.message/);
});

test("remote connection and offline surfaces use the common locale catalog", () => {
  const health = readFileSync("src/hooks/use-remote-health.ts", "utf8");
  const overlay = readFileSync(
    "src/components/layout/offline-overlay.tsx",
    "utf8",
  );
  const zh = JSON.parse(readFileSync("src/i18n/locales/zh/common.json", "utf8"));
  const en = JSON.parse(readFileSync("src/i18n/locales/en/common.json", "utf8"));
  const keys = [
    "remoteConnectionLost",
    "remoteAuthenticationFailed",
    "remoteReconnected",
    "offlineUnableToConnect",
    "offlineTunnelHint",
    "offlineBackendHint",
    "offlineRetry",
    "offlineRescan",
    "offlineDismiss",
  ];

  assert.match(health, /i18n\.t\("remoteConnectionLost", \{ ns: "common" \}\)/);
  assert.match(overlay, /useTranslation\("common"\)/);
  for (const key of keys) {
    assert.equal(typeof zh[key], "string");
    assert.equal(typeof en[key], "string");
    assert.ok(zh[key].length > 0);
    assert.ok(en[key].length > 0);
  }
});

test("artifact and manual compaction surfaces keep raw API details diagnostic-only", () => {
  const renderers = [
    "docx-renderer.tsx",
    "xlsx-renderer.tsx",
    "pptx-renderer.tsx",
    "pdf-renderer.tsx",
    "image-renderer.tsx",
    "media-renderer.tsx",
    "file-preview-renderer.tsx",
  ];
  for (const renderer of renderers) {
    const source = readFileSync(
      `src/components/artifacts/renderers/${renderer}`,
      "utf8",
    );
    assert.doesNotMatch(source, /apiErrorMessage/);
  }

  const compaction = readFileSync(
    "src/components/chat/context-indicator.tsx",
    "utf8",
  );
  assert.doesNotMatch(compaction, /errorToMessage/);
  assert.match(compaction, /toast\.error\(t\("contextCompactError"\)/);
});

test("batch progress, tool failures, and background notifications use safe localized text", () => {
  const batch = readFileSync("../backend/app/session/task_batch.py", "utf8");
  const progress = readFileSync(
    "src/components/workspace/progress-section.tsx",
    "utf8",
  );
  const registry = readFileSync("src/lib/session-stream-registry.ts", "utf8");

  assert.doesNotMatch(batch, /state\.error = str\(exc\)/);
  assert.doesNotMatch(progress, /task\.error \|\|/);
  assert.match(registry, /i18n\.t\("statusError", \{ ns: "chat" \}\)/);
  assert.match(registry, /i18n\.t\("backgroundTaskFinished"/);
  assert.match(registry, /i18n\.t\("backgroundTaskStopped"/);
  assert.doesNotMatch(registry, /sessionTitle = .*"Background task"/);
});
