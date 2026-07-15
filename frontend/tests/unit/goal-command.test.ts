import assert from "node:assert/strict";
import test from "node:test";

import {
  GOAL_OBJECTIVE_MAX_CHARACTERS,
  countGoalObjectiveCharacters,
  parseGoalCommand,
  type GoalCommandAction,
} from "../../src/lib/goal-command.ts";

test("bare goal roots view the current goal and English is case-insensitive", () => {
  assert.deepEqual(parseGoalCommand("/目标"), {
    ok: true,
    command: "目标",
    action: "view",
  });
  assert.deepEqual(parseGoalCommand("/goal   \n\t"), {
    ok: true,
    command: "goal",
    action: "view",
  });
  assert.deepEqual(parseGoalCommand("/GoAl"), {
    ok: true,
    command: "goal",
    action: "view",
  });
});

test("implicit create trims only the objective boundary and preserves multiline content", () => {
  assert.deepEqual(
    parseGoalCommand("/goal   Ship the report\n  Keep this indentation\n\nVerify totals   "),
    {
      ok: true,
      command: "goal",
      action: "create",
      objective: "Ship the report\n  Keep this indentation\n\nVerify totals",
    },
  );
  assert.deepEqual(parseGoalCommand("/目标\n生成报告\n核对总数"), {
    ok: true,
    command: "目标",
    action: "create",
    objective: "生成报告\n核对总数",
  });
});

test("explicit create and edit accept Chinese or English aliases under either root", () => {
  const cases: Array<[string, "create" | "edit", string]> = [
    ["/goal create Alpha", "create", "Alpha"],
    ["/GOAL NEW Alpha", "create", "Alpha"],
    ["/goal set Alpha", "create", "Alpha"],
    ["/目标 新建 Alpha", "create", "Alpha"],
    ["/goal 创建 Alpha", "create", "Alpha"],
    ["/目标 设定 Alpha", "create", "Alpha"],
    ["/goal edit Beta", "edit", "Beta"],
    ["/GOAL UPDATE Beta", "edit", "Beta"],
    ["/目标 change Beta", "edit", "Beta"],
    ["/goal 编辑 Beta", "edit", "Beta"],
    ["/目标 修改 Beta", "edit", "Beta"],
  ];

  for (const [input, action, objective] of cases) {
    const parsed = parseGoalCommand(input);
    assert.equal(parsed?.ok, true, input);
    assert.equal(parsed?.action, action, input);
    if (parsed?.ok) assert.equal(parsed.objective, objective, input);
  }

  assert.deepEqual(parseGoalCommand("/goal edit\n  First line\n    Second line  "), {
    ok: true,
    command: "goal",
    action: "edit",
    objective: "First line\n    Second line",
  });
});

test("view, pause, resume, and clear aliases are exact and case-insensitive", () => {
  const cases: Array<[string, GoalCommandAction]> = [
    ["/goal view", "view"],
    ["/目标 查看", "view"],
    ["/goal SHOW", "view"],
    ["/目标 状态", "view"],
    ["/goal pause", "pause"],
    ["/目标 暂停", "pause"],
    ["/goal resume", "resume"],
    ["/GOAL CONTINUE", "resume"],
    ["/目标 继续", "resume"],
    ["/goal 恢复", "resume"],
    ["/goal clear", "clear"],
    ["/GOAL DELETE", "clear"],
    ["/目标 清除", "clear"],
    ["/goal 删除", "clear"],
  ];

  for (const [input, action] of cases) {
    const parsed = parseGoalCommand(input);
    assert.equal(parsed?.ok, true, input);
    assert.equal(parsed?.action, action, input);
  }
});

test("the root command must start at character zero and have an exact boundary", () => {
  const ordinaryMessages = [
    "/goals",
    "/goalkeeper finish this",
    "/goal: finish this",
    "/目标化管理",
    "//goal finish this",
    "//目标 完成报告",
    " /goal finish this",
    "\n/goal finish this",
    "Please use /goal finish this",
    "请使用 /目标 完成报告",
    "Body first\n/goal finish this",
  ];

  for (const input of ordinaryMessages) {
    assert.equal(parseGoalCommand(input), null, input);
  }
});

test("explicit objective actions reject missing objectives", () => {
  for (const input of ["/goal create", "/目标 新建   ", "/goal EDIT\n", "/目标 编辑"] as const) {
    const parsed = parseGoalCommand(input);
    assert.equal(parsed?.ok, false, input);
    if (!parsed?.ok) assert.equal(parsed?.error, "objective_required", input);
  }
});

test("actions without an objective reject trailing arguments instead of leaking to chat", () => {
  for (const input of [
    "/goal view extra",
    "/目标 暂停 now",
    "/goal resume\nextra",
    "/目标 清除 这个",
  ]) {
    const parsed = parseGoalCommand(input);
    assert.equal(parsed?.ok, false, input);
    if (!parsed?.ok) assert.equal(parsed?.error, "unexpected_argument", input);
  }
});

test("objective length uses Unicode characters and enforces the 4000-character limit", () => {
  assert.equal(GOAL_OBJECTIVE_MAX_CHARACTERS, 4_000);
  assert.equal(countGoalObjectiveCharacters("🐂"), 1);

  const exactlyAtLimit = "🐂".repeat(GOAL_OBJECTIVE_MAX_CHARACTERS);
  const accepted = parseGoalCommand(`/goal ${exactlyAtLimit}`);
  assert.equal(accepted?.ok, true);
  if (accepted?.ok) assert.equal(accepted.objective, exactlyAtLimit);

  const overLimit = `${exactlyAtLimit}有`;
  assert.deepEqual(parseGoalCommand(`/目标 编辑 ${overLimit}`), {
    ok: false,
    command: "目标",
    action: "edit",
    error: "objective_too_long",
    objectiveCharacters: 4_001,
    maxObjectiveCharacters: 4_000,
  });
});

test("near-action words remain valid implicit objectives", () => {
  assert.deepEqual(parseGoalCommand("/goal paused work should be documented"), {
    ok: true,
    command: "goal",
    action: "create",
    objective: "paused work should be documented",
  });
  assert.deepEqual(parseGoalCommand("/目标 编辑器完成后导出"), {
    ok: true,
    command: "目标",
    action: "create",
    objective: "编辑器完成后导出",
  });
});
