import assert from "node:assert/strict";
import test from "node:test";

import {
  getToolDisplayTitle,
  localizeVisibleProcessText,
  translatePersistedToolOutput,
  translatePersistedToolTitle,
} from "../../src/lib/activity-labels.ts";
import type { ToolPart } from "../../src/types/message.ts";

function t(key: string, options?: Record<string, unknown>): string {
  const map: Record<string, string> = {
    file: "文件",
    toolApplyingPatch: "正在应用补丁",
    toolRunCommand: "执行命令",
    toolSearchFiles: "搜索文件",
    toolSearch: `搜索：${options?.query ?? ""}`,
    toolFetch: `获取 ${options?.url ?? ""}`,
    toolWebSearch: `搜索网页：${options?.query ?? ""}`,
    toolSubtask: "子任务",
    toolAskQuestionShort: "正在提问",
    toolUpdateProgress: "更新进度",
  };
  return map[key] ?? key;
}

function toolPart(tool: string, title: string | null, input: Record<string, unknown> = {}): ToolPart {
  return {
    type: "tool",
    tool,
    call_id: `${tool}-1`,
    state: {
      status: "completed",
      input,
      output: null,
      metadata: null,
      title,
      time_start: null,
      time_end: null,
      time_compacted: null,
    },
  };
}

test("translates persisted English tool titles shown in activity history", () => {
  assert.equal(getToolDisplayTitle(toolPart("skill", "Loaded skill: presentation"), t, "zh"), "已加载技能：presentation");
  assert.equal(getToolDisplayTitle(toolPart("todo", "Todo list"), t, "zh"), "待办清单");
  assert.equal(getToolDisplayTitle(toolPart("write", "Created report.md"), t, "zh"), "已创建 report.md");
  assert.equal(getToolDisplayTitle(toolPart("present_file", "Presented Final Report"), t, "zh"), "已展示 Final Report");
  assert.equal(getToolDisplayTitle(toolPart("web_search", "Search: 苏州 (3 results)"), t, "zh"), "搜索：苏州（3 条结果）");
});

test("translates persisted Chinese UI framing for English activity history", () => {
  assert.equal(getToolDisplayTitle(toolPart("skill", "已加载技能：presentation"), t, "en"), "Loaded skill: presentation");
  assert.equal(getToolDisplayTitle(toolPart("todo", "待办清单"), t, "en"), "Todo list");
  assert.equal(getToolDisplayTitle(toolPart("write", "已创建 report.md"), t, "en"), "Created report.md");
  assert.equal(getToolDisplayTitle(toolPart("web_search", "搜索：苏州（3 条结果）"), t, "en"), "Search: 苏州 (3 results)");
  assert.equal(getToolDisplayTitle(toolPart("write", "Created report.md"), t, "en"), "Created report.md");
});

test("translates every current backend activity-title variant bidirectionally", () => {
  const pairs = [
    ["Edited report.md (2 replacements)", "已编辑 report.md（2 处替换）"],
    [
      "Edited report.md (3 edits, 5 replacements)",
      "已编辑 report.md（3 个编辑，5 处替换）",
    ],
    ["3 files match **/*.ts", "3 个文件匹配 **/*.ts"],
    ["3 matches /TODO/", "3 处匹配 /TODO/"],
    ['2 search results for "abc"', "2 条“abc”的搜索结果"],
    ["Subtask (explore): inspect", "子任务（explore）：inspect"],
    ["Tool search", "工具搜索"],
    ["Listed 4 entries in src", "已列出 src 中的 4 个条目"],
  ] as const;

  for (const [english, chinese] of pairs) {
    assert.equal(translatePersistedToolTitle(english, "zh"), chinese);
    assert.equal(translatePersistedToolTitle(chinese, "en"), english);
  }
});

test("keeps legacy persisted activity-title variants localized", () => {
  assert.equal(
    translatePersistedToolTitle("3 files matching **/*.ts", "zh"),
    "3 个文件匹配 **/*.ts",
  );
  assert.equal(
    translatePersistedToolTitle("3 matches for TODO", "zh"),
    "3 处匹配 TODO",
  );
  assert.equal(
    translatePersistedToolTitle("SubAgent (explore): inspect", "zh"),
    "子任务（explore）：inspect",
  );
});

test("localizes only known app-generated tool-output wrappers", () => {
  const pairs = [
    ["todo", "Todo list updated: 1/3 completed, 1 in progress, 1 pending", "待办清单已更新：已完成 1/3，1 个进行中，1 个待处理"],
    ["write", "Created /tmp/报告.md (2 lines)", "已创建 /tmp/报告.md（2 行）"],
    ["web_search", "No results found.", "未找到结果。"],
    ["question", "[No user connected] Asked: 保留 user text?", "[没有用户连接] 已提问：保留 user text?"],
    ["question", "[No user connected] [Multiple questions] 2 questions", "[没有用户连接] [多问题] 2 个问题"],
    [
      "plan",
      "Switched to build mode; full tool access has been restored.",
      "已切换到构建模式，完整工具权限已恢复。",
    ],
    ["submit_plan", "[No user connected] Submitted plan: 发布 rc.2", "[没有用户连接] 已提交计划：发布 rc.2"],
  ] as const;

  for (const [tool, english, chinese] of pairs) {
    assert.equal(translatePersistedToolOutput(tool, english, "zh"), chinese);
    assert.equal(translatePersistedToolOutput(tool, chinese, "en"), english);
  }

  const userContent = "用户文件内容：不要翻译 / User-authored content";
  assert.equal(translatePersistedToolOutput("read", userContent, "en"), userContent);
  assert.equal(translatePersistedToolOutput("web_search", userContent, "en"), userContent);
});

test("localizes structured tool wrappers while preserving embedded content", () => {
  const acceptedPlan =
    "The user accepted the plan (mode: auto). Switch to build mode and execute it:\n\n# 计划\nKeep this body unchanged.";
  assert.equal(
    translatePersistedToolOutput("submit_plan", acceptedPlan, "zh"),
    "用户已接受计划（模式：auto）。切换到构建模式并执行计划：\n\n# 计划\nKeep this body unchanged.",
  );

  const taskOutput =
    "用户 summary stays\n\n--- Key tool results ---\n[read] 文件 content\n[Errors: original 错误]";
  assert.equal(
    translatePersistedToolOutput("task", taskOutput, "zh"),
    "用户 summary stays\n\n--- 关键工具结果 ---\n[read] 文件 content\n[错误：original 错误]",
  );

  const patchOutput = [
    "+ Added report.md",
    "~ Updated src/app.ts",
    "",
    "@@ -1 +1 @@",
    "+ Added user-authored line",
  ].join("\n");
  assert.equal(
    translatePersistedToolOutput("apply_patch", patchOutput, "zh"),
    [
      "+ 已新增 report.md",
      "~ 已更新 src/app.ts",
      "",
      "@@ -1 +1 @@",
      "+ Added user-authored line",
    ].join("\n"),
  );

  assert.equal(
    translatePersistedToolOutput("artifact", 'Created artifact "季度报告".', "zh"),
    "已创建制品“季度报告”。",
  );
  assert.equal(
    translatePersistedToolOutput("present_file", "已展示 /tmp/报告.pdf", "en"),
    "Presented /tmp/报告.pdf",
  );
});

test("uses localized fallback labels when no persisted title exists", () => {
  assert.equal(getToolDisplayTitle(toolPart("apply_patch", null), t, "zh"), "正在应用补丁");
  assert.equal(
    getToolDisplayTitle(toolPart("task", null, { description: "整理资料" }), t, "zh"),
    "整理资料",
  );
  assert.equal(
    getToolDisplayTitle(toolPart("grep", null, { pattern: "Loaded skill" }), t, "zh"),
    "搜索：Loaded skill",
  );
});

test("localizes English reasoning trace text for Chinese UI", () => {
  const englishTrace = [
    "The search tool seems to be having issues. Let me try using web_fetch to access some known sources about this project.",
    "Search is failing. Let me try using web_fetch on some known URLs about this topic.",
    "Search is failing. Let me try using web_fetch on some known URLs about this topic. The 暖心饭卡 project is quite well-known in China. Let me try fetching from Baidu Baike or other news sources.",
    "Let me now search for academic papers related to this project. Let me try searching in academic databases.",
  ].join("\n");

  assert.equal(
    localizeVisibleProcessText(englishTrace, "zh"),
    [
      "搜索工具似乎不稳定，改用 web_fetch 访问相关来源。",
      "搜索请求失败，改用 web_fetch 访问相关网址继续核验。",
      "搜索请求失败，改用 web_fetch 访问相关网址继续核验。",
      "继续查找该项目相关论文和学术成果。",
    ].join("\n"),
  );
});

test("keeps code fences and non-Chinese locales unchanged when localizing process text", () => {
  const mixedTrace = [
    "Let me inspect the implementation.",
    "```ts",
    "const status = \"Search is failing\";",
    "```",
  ].join("\n");

  assert.equal(
    localizeVisibleProcessText(mixedTrace, "zh"),
    [
      "正在检查实现细节。",
      "```ts",
      "const status = \"Search is failing\";",
      "```",
    ].join("\n"),
  );
  assert.equal(localizeVisibleProcessText(mixedTrace, "en"), mixedTrace);
});
