import assert from "node:assert/strict";
import test from "node:test";

import { getToolDisplayTitle, localizeVisibleProcessText } from "../../src/lib/activity-labels.ts";
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
  assert.equal(getToolDisplayTitle(toolPart("skill", "Loaded skill: presentation"), t), "已加载技能：presentation");
  assert.equal(getToolDisplayTitle(toolPart("todo", "Todo list"), t), "待办清单");
  assert.equal(getToolDisplayTitle(toolPart("write", "Created report.md"), t), "已创建 report.md");
  assert.equal(getToolDisplayTitle(toolPart("present_file", "Presented Final Report"), t), "已展示 Final Report");
  assert.equal(getToolDisplayTitle(toolPart("web_search", "Search: 苏州 (3 results)"), t), "搜索：苏州（3 条结果）");
});

test("uses localized fallback labels when no persisted title exists", () => {
  assert.equal(getToolDisplayTitle(toolPart("apply_patch", null), t), "正在应用补丁");
  assert.equal(
    getToolDisplayTitle(toolPart("task", null, { description: "整理资料" }), t),
    "整理资料",
  );
  assert.equal(
    getToolDisplayTitle(toolPart("grep", null, { pattern: "Loaded skill" }), t),
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
