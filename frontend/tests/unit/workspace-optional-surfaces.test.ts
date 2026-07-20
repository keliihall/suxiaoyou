import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";


test("optional workspace surfaces handle unavailable and provenance states", () => {
  const runtimeSource = readFileSync(
    "src/components/workspace/runtime-control-card.tsx",
    "utf8",
  );
  const officeSource = readFileSync(
    "src/components/workspace/user-office-template-card.tsx",
    "utf8",
  );
  const zh = JSON.parse(readFileSync("src/i18n/locales/zh/chat.json", "utf8"));

  assert.match(runtimeSource, /code === "runtime_workspace_not_found"/);
  assert.match(runtimeSource, /t\("runtimeWorkspaceIdentityMismatch"\)/);
  assert.match(runtimeSource, /worktree_creation_available/);
  assert.match(runtimeSource, /runtimeAdvancedOptions/);
  assert.match(runtimeSource, /worktreeErrorLabelKey/);
  assert.doesNotMatch(runtimeSource, /apiErrorMessage/);
  assert.doesNotMatch(runtimeSource, /\{context\.workspace_kind\}/);
  assert.doesNotMatch(runtimeSource, /\{checkpoint\.state\}/);
  assert.doesNotMatch(runtimeSource, />Beta</);
  assert.doesNotMatch(runtimeSource, /workspace_kind === "worktree"/);
  assert.match(runtimeSource, /workspace_kind === "git_worktree"/);
  assert.match(
    officeSource,
    /code === "user_office_template_runtime_unavailable"/,
  );
  assert.match(
    officeSource,
    /code === "runtime_workspace_provenance_mismatch"/,
  );
  assert.equal(
    zh.runtimeWorkspaceIdentityMismatch,
    "当前文件夹与已保存的版本记录不一致，请重新选择文件夹后重试。",
  );
  assert.equal(zh.runtimeControlTitle, "版本与恢复");
  assert.equal(zh.runtimeCheckpointLabel, "版本 {{sequence}}");
  assert.equal(zh.runtimeRewind, "恢复到这里");
  const visibleRuntimeChinese = Object.entries(zh)
    .filter(([key]) => key.startsWith("runtime"))
    .map(([, value]) => String(value))
    .join("\n");
  assert.doesNotMatch(visibleRuntimeChinese, /\b(?:Checkpoint|Rewind|worktree|Beta|direct|finalized)\b/i);
});
