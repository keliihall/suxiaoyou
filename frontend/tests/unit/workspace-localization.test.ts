import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

function readJson<T>(path: string): T {
  return JSON.parse(readFileSync(path, "utf8")) as T;
}

test("workspace context panel uses localized visible labels", () => {
  const source = readFileSync("src/components/workspace/context-section.tsx", "utf8");
  const hardcodedEnglish = [
    "Workspace-aware context",
    "Waiting for workspace",
    "No connectors active",
    "skills available",
    "No memory yet",
    "Memory saved",
    "Failed to save memory",
    "Failed to refresh memory",
    "Failed to export",
  ];

  for (const phrase of hardcodedEnglish) {
    assert.doesNotMatch(source, new RegExp(phrase.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")));
  }

  const zhChat = readJson<Record<string, string>>("src/i18n/locales/zh/chat.json");
  assert.equal(zhChat.contextPanel, "上下文");
  assert.equal(zhChat.workspaceAwareContext, "工作区感知上下文");
  assert.equal(zhChat.waitingForWorkspace, "等待工作区");
  assert.equal(zhChat.noConnectorsActive, "暂无启用的连接器");
  assert.equal(zhChat.skillsAvailable, "{{count}} 个技能可用");
  assert.equal(zhChat.noMemoryYet, "暂无记忆");
});

test("Chinese landing greeting uses the requested companionship wording", () => {
  const zhChat = readJson<Record<string, string>>("src/i18n/locales/zh/chat.json");

  assert.equal(zhChat.greeting, "今天，让苏小有陪你一起完成什么？");
});

test("multi-agent task batch popover uses localized visible labels", () => {
  const source = readFileSync("src/components/chat/chat-form.tsx", "utf8");
  const hardcodedEnglish = [
    "Multi-agent tasks",
    "Use input",
    "Add task",
    "Task title",
    "Remove task",
    "Prompt for this agent",
    "Current model",
    "Task batches do not support attachments yet.",
    "Each task needs a title and prompt.",
    "Start batch",
  ];

  for (const phrase of hardcodedEnglish) {
    assert.doesNotMatch(source, new RegExp(phrase.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")));
  }

  const zhChat = readJson<Record<string, string>>("src/i18n/locales/zh/chat.json");
  assert.equal(zhChat.taskBatchButton, "多智能体任务");
  assert.equal(zhChat.taskBatchModeParallelShort, "并行");
  assert.equal(zhChat.taskBatchModeSequentialShort, "顺序");
  assert.equal(zhChat.taskBatchUseInput, "使用输入内容");
  assert.equal(zhChat.taskBatchAddTask, "添加任务");
  assert.equal(zhChat.taskBatchTaskTitlePlaceholder, "任务标题");
  assert.equal(zhChat.taskBatchPromptPlaceholder, "给这个智能体的提示");
  assert.equal(zhChat.taskBatchStart, "开始批量任务");
});

test("general settings typography controls and sample are localized", () => {
  const generalTab = readFileSync("src/components/settings/general-tab.tsx", "utf8");
  const appearanceCustomize = readFileSync("src/components/settings/appearance-customize.tsx", "utf8");
  assert.match(generalTab, /TYPOGRAPHY_SAMPLE_EN/);
  assert.match(generalTab, /TYPOGRAPHY_SAMPLE_ZH/);
  assert.match(generalTab, /i18n\.resolvedLanguage\?\.startsWith\("zh"\)/);
  assert.doesNotMatch(generalTab, /label:\s*"Serif"/);
  assert.doesNotMatch(generalTab, /label:\s*"Sans-serif"/);
  assert.doesNotMatch(appearanceCustomize, /\n\s*reset\s*\n/);

  const zhSettings = readJson<Record<string, string>>("src/i18n/locales/zh/settings.json");
  const enSettings = readJson<Record<string, string>>("src/i18n/locales/en/settings.json");
  assert.equal(zhSettings.typographyPreview, "排版预览");
  assert.equal(zhSettings.serifFont, "衬线");
  assert.equal(zhSettings.sansSerifFont, "无衬线");
  assert.equal(zhSettings.resetColor, "重置");
  assert.equal(enSettings.typographyPreview, "Typography preview");
  assert.equal(enSettings.serifFont, "Serif");
  assert.equal(enSettings.sansSerifFont, "Sans serif");
  assert.equal(enSettings.resetColor, "Reset");
  assert.match(generalTab, /# 一级标题/);
  assert.match(generalTab, /# Level-one heading/);
});

test("sidebar relative time labels are localized", () => {
  const source = readFileSync("src/components/layout/session-item.tsx", "utf8");
  const utils = readFileSync("src/lib/utils.ts", "utf8");
  const sessionList = readFileSync("src/components/layout/session-list.tsx", "utf8");
  const zhCommon = readJson<Record<string, string>>("src/i18n/locales/zh/common.json");
  const enCommon = readJson<Record<string, string>>("src/i18n/locales/en/common.json");

  assert.match(source, /formatRelativeTime/);
  assert.match(utils, /刚刚/);
  assert.match(utils, /分钟前/);
  assert.match(utils, /小时前/);
  assert.match(utils, /昨天/);
  assert.match(utils, /前天/);
  assert.doesNotMatch(utils, /\$\{days\}天前/);
  assert.doesNotMatch(utils, /周前/);
  assert.match(source, /sessionRunning/);
  assert.match(source, /sessionCreatedAt/);
  assert.match(source, /sessionUpdatedAt/);
  assert.match(source, /isLive \|\| showTimestamp \? "pr-16" : "pr-2"/);
  assert.match(sessionList, /showTimestamp=\{hasSearch \|\| organizeMode !== "chronological"\}/);
  assert.equal(zhCommon.sessionRunning, "进行中");
  assert.equal(enCommon.sessionRunning, "Running");
  assert.doesNotMatch(source, /return "now"/);
  assert.doesNotMatch(source, /`\$\{minutes\}m`/);
  assert.doesNotMatch(source, /`\$\{hours\}h`/);
});

test("session time display treats backend naive timestamps as UTC and displays in Shanghai time", () => {
  const utils = readFileSync("src/lib/utils.ts", "utf8");
  const sessionItem = readFileSync("src/components/layout/session-item.tsx", "utf8");
  const mobilePage = readFileSync("src/app/(mobile)/m/page.tsx", "utf8");

  assert.match(utils, /APP_TIME_ZONE\s*=\s*"Asia\/Shanghai"/);
  assert.match(utils, /parseBackendDate/);
  assert.match(utils, /trimmed \+ "Z"/);
  assert.match(sessionItem, /getSessionTimestamp/);
  assert.match(sessionItem, /formatFullDateTime/);
  assert.match(sessionItem, /formatRelativeTime/);
  assert.match(mobilePage, /formatRelativeTime/);
  assert.doesNotMatch(sessionItem, /new Date\(date\)/);
  assert.doesNotMatch(mobilePage, /new Date\(dateStr\)/);
});
