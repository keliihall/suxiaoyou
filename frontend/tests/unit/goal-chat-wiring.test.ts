import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const useChat = readFileSync("src/hooks/use-chat.ts", "utf8");
const chatView = readFileSync("src/components/chat/chat-view.tsx", "utf8");
const landing = readFileSync("src/components/chat/landing.tsx", "utf8");
const frontendPackage = JSON.parse(readFileSync("package.json", "utf8")) as {
  scripts?: Record<string, string>;
};

function between(source: string, startNeedle: string, endNeedle: string): string {
  const start = source.indexOf(startNeedle);
  const end = source.indexOf(endNeedle, start + startNeedle.length);
  assert.notEqual(start, -1, `missing start boundary: ${startNeedle}`);
  assert.notEqual(end, -1, `missing end boundary: ${endNeedle}`);
  return source.slice(start, end);
}

test("useChat dispatches every parsed Goal command through the control plane", () => {
  const handler = between(
    useChat,
    "const handleGoalCommand = useCallback",
    "const sendMessage = useCallback",
  );
  for (const action of ["view", "create", "edit", "pause", "resume"]) {
    assert.match(handler, new RegExp(`command\\.action === \\\"${action}\\\"`));
  }
  assert.match(handler, /clearSessionGoal\(targetSessionId/);
  assert.match(handler, /goalClearPauseFirst/);
  assert.match(handler, /startGoal\(\{/);
  assert.match(handler, /writeGoalSnapshot\(queryClient, response\.goal\)/);
  assert.match(handler, /startGeneration\(response\.session_id, response\.stream_id\)/);
  assert.match(handler, /startStream\(response\.session_id, response\.stream_id\)/);
  assert.match(handler, /router\.push\(getChatRoute\(response\.session_id\)\)/);
  assert.match(handler, /goalRequestScope = `goal-start:/);
  assert.match(handler, /reservePromptRequestId\([\s\S]*promptRequestFingerprint\(payload\)/);
  assert.match(handler, /clearPromptRequestId\(goalRequestScope, clientRequestId\)/);
  assert.match(handler, /return reportFailure\(error\)/);
});

test("Goal creation forwards attachments without weakening image capability checks", () => {
  const handler = between(
    useChat,
    "const handleGoalCommand = useCallback",
    "const sendMessage = useCallback",
  );
  assert.match(
    handler,
    /command: ParsedGoalCommand,[\s\S]*attachments\?: FileAttachment\[\]/,
  );
  assert.match(handler, /hasImageAttachments\(attachments\)/);
  assert.match(handler, /selectedModelSupportsVision\(/);
  assert.match(handler, /attachments: attachments \?\? \[\]/);
  assert.match(handler, /beginSending\(targetSessionId, objective, attachments\)/);
  assert.match(
    handler,
    /if \(attachments\?\.length && command\.action !== "create"\)[\s\S]*goalCommandAttachmentsUnsupported/,
  );
});

test("all shared ChatForm instances receive the Goal dispatcher", () => {
  assert.equal((chatView.match(/<ChatForm/g) ?? []).length, 1);
  assert.equal((chatView.match(/onGoalCommand=\{handleGoalCommand\}/g) ?? []).length, 1);
  assert.equal((landing.match(/<ChatForm/g) ?? []).length, 2);
  assert.equal((landing.match(/onGoalCommand=\{handleGoalCommand\}/g) ?? []).length, 2);
});

test("the CI-facing core UI suite includes the Goal control-plane E2E", () => {
  assert.match(
    frontendPackage.scripts?.["test:ui:core"] ?? "",
    /tests\/ui\/goal-mode\.spec\.ts/,
  );
});

test("Stop safely pauses the latest matching active Goal and keeps abort for ordinary streams", () => {
  const stop = between(
    useChat,
    "const stopGeneration = useCallback",
    "const reconnectGeneration = useCallback",
  );
  const latestRead = stop.indexOf("latestGoal = await getSessionGoal(targetSessionId)");
  const streamMatch = stop.indexOf("isActiveGoalStream(latestGoal, streamId)");
  const pause = stop.indexOf("pauseSessionGoal(targetSessionId");
  const abort = stop.indexOf("API.CHAT.ABORT");
  assert.ok(latestRead >= 0 && latestRead < streamMatch);
  assert.ok(streamMatch < pause && pause < abort);
  assert.match(stop, /expected_revision: latestGoal\.revision/);
  assert.match(stop, /Keep SSE attached[\s\S]*return true;/);
  assert.match(stop, /if \(isActiveGoalStream\(cachedGoal, streamId\)\)[\s\S]*return false;/);
});

test("every optimistic SessionResponse carries Goal summary fields", () => {
  const tempSessionCount = (useChat.match(/const tempSession: SessionResponse/g) ?? []).length;
  assert.equal(tempSessionCount, 3);
  for (const field of [
    "goal_status:",
    "goal_run_state:",
    "goal_needs_input:",
    "goal_objective_preview:",
  ]) {
    assert.equal((useChat.match(new RegExp(field, "g")) ?? []).length >= 3, true, field);
  }
});
