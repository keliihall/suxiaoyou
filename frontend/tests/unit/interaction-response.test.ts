import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  advanceInteractionResponseState,
  advanceInteractionRecoveryRetryState,
  canMarkInteractionContinuing,
  canResetInteractionAfterFailure,
  canSubmitInteraction,
  isInteractionContinuationEvent,
  isInteractionAwaitingResolution,
  isInteractionPendingContinuation,
  isInteractionRecoveryActionable,
  matchesInteractionRecoveryTarget,
} from "../../src/lib/interaction-response.ts";

test("interaction acknowledgement state is monotonic across HTTP/SSE races", () => {
  assert.equal(canSubmitInteraction(undefined), true);
  assert.equal(canSubmitInteraction("idle"), true);
  assert.equal(canSubmitInteraction("submitting"), false);
  assert.equal(canSubmitInteraction("resolved"), false);
  assert.equal(canSubmitInteraction("continuing"), false);
  assert.equal(canSubmitInteraction("recovery_needed"), false);
  assert.equal(isInteractionAwaitingResolution(undefined), true);
  assert.equal(isInteractionAwaitingResolution("submitting"), true);
  assert.equal(isInteractionAwaitingResolution("resolved"), false);
  assert.equal(isInteractionAwaitingResolution("continuing"), false);
  assert.equal(isInteractionAwaitingResolution("recovering"), false);
  assert.equal(canResetInteractionAfterFailure("submitting"), true);
  assert.equal(canResetInteractionAfterFailure("resolved"), false);
  assert.equal(canResetInteractionAfterFailure("continuing"), false);

  assert.equal(advanceInteractionResponseState("idle", "submitting"), "submitting");
  assert.equal(advanceInteractionResponseState("submitting", "resolved"), "resolved");
  assert.equal(advanceInteractionResponseState("resolved", "continuing"), "continuing");
  assert.equal(advanceInteractionResponseState("resolved", "recovering"), "recovering");
  assert.equal(
    advanceInteractionResponseState("recovering", "recovery_needed"),
    "recovery_needed",
  );
  assert.equal(
    advanceInteractionResponseState("recovery_needed", "recovering"),
    "recovery_needed",
    "late ordinary events cannot restart recovery",
  );
  assert.equal(
    advanceInteractionRecoveryRetryState("recovery_needed"),
    "recovering",
  );
  assert.equal(
    advanceInteractionRecoveryRetryState("recovering"),
    "recovering",
  );
  assert.equal(
    advanceInteractionRecoveryRetryState("continuing"),
    "continuing",
  );
  assert.equal(
    advanceInteractionResponseState("continuing", "resolved"),
    "continuing",
    "a late POST response must not overwrite a continuation SSE event",
  );
});

test("interaction recovery is gated by session, stream, call and generation", () => {
  const expected = {
    sessionId: "session-a",
    streamId: "stream-new",
    callId: "call-new",
    promptType: "question" as const,
    streamGeneration: 7,
  };

  assert.equal(matchesInteractionRecoveryTarget(expected, { ...expected }), true);
  assert.equal(
    matchesInteractionRecoveryTarget(expected, { ...expected, streamId: "stream-old" }),
    false,
  );
  assert.equal(
    matchesInteractionRecoveryTarget(expected, { ...expected, callId: "call-old" }),
    false,
  );
  assert.equal(
    matchesInteractionRecoveryTarget(expected, { ...expected, streamGeneration: 6 }),
    false,
  );
  assert.equal(isInteractionPendingContinuation("resolved"), true);
  assert.equal(isInteractionRecoveryActionable("recovery_needed"), true);
  assert.equal(isInteractionRecoveryActionable("recovering"), false);
  assert.equal(canMarkInteractionContinuing("submitting"), true);
  assert.equal(isInteractionContinuationEvent("text-delta"), true);
  assert.equal(isInteractionContinuationEvent("permission-resolved"), false);
  assert.equal(isInteractionContinuationEvent("heartbeat"), false);
});

test("permission, question and plan keep their cards through acknowledgement", () => {
  const hook = readFileSync("src/hooks/use-chat.ts", "utf8");
  const registry = readFileSync("src/lib/session-stream-registry.ts", "utf8");
  const permission = readFileSync(
    "src/components/interactive/permission-dialog.tsx",
    "utf8",
  );
  const question = readFileSync(
    "src/components/interactive/question-prompt.tsx",
    "utf8",
  );
  const plan = readFileSync(
    "src/components/interactive/plan-accept-prompt.tsx",
    "utf8",
  );
  const chatView = readFileSync("src/components/chat/chat-view.tsx", "utf8");
  const acknowledgement = readFileSync(
    "src/components/interactive/interaction-acknowledgement.tsx",
    "utf8",
  );
  const chatStore = readFileSync("src/stores/chat-store.ts", "utf8");

  assert.match(hook, /"permission"[\s\S]*"submitting"[\s\S]*"resolved"/);
  assert.match(hook, /"question"[\s\S]*"submitting"[\s\S]*"resolved"/);
  assert.match(hook, /"plan"[\s\S]*"submitting"[\s\S]*"resolved"/);
  assert.doesNotMatch(hook, /clearQuestion\(targetSessionId\)/);
  assert.doesNotMatch(hook, /clearPermissionRequest\(targetSessionId\)/);
  assert.doesNotMatch(hook, /clearPlanReview\(targetSessionId\)/);

  assert.match(registry, /PLAN_REVIEW_RESOLVED/);
  assert.match(registry, /markInteractionContinuing/);
  assert.match(registry, /INTERACTION_CONTINUATION_GRACE_MS/);
  assert.match(registry, /matchesInteractionRecoveryTarget/);
  assert.match(registry, /API\.CHAT\.ACTIVE/);
  assert.match(registry, /"recovery_needed"/);
  assert.match(registry, /interactionRecoverySequence/);
  assert.match(registry, /pending\.responseState !== "recovery_needed"/);
  assert.match(registry, /beginInteractionRecoveryRetry/);
  assert.match(registry, /instance\.client\.reconnectNow\(\)/);
  assert.match(chatStore, /responseResolvedAt:[\s\S]*Date\.now\(\)/);
  assert.match(acknowledgement, /interactionRecoverAction/);
  assert.match(acknowledgement, /isInteractionRecoveryActionable\(state\)/);
  assert.match(acknowledgement, /interactionStopAction/);
  assert.match(registry, /responseState === "continuing"/);
  for (const source of [permission, question, plan]) {
    assert.match(source, /InteractionAcknowledgement/);
  }
  assert.doesNotMatch(question, /setAnswer\(""\)/);
  assert.doesNotMatch(plan, /setFeedback\(""\)/);
  assert.match(
    chatView,
    /enabled: !pendingPermission && !pendingQuestion && !pendingPlanReview/,
  );
  assert.match(
    chatView,
    /pendingPlanReview\.responseState === "recovery_needed"[\s\S]*<ChatForm/,
  );
  assert.doesNotMatch(chatView, /<PermissionDialog[\s\S]{0,300}onStop=/);
  assert.doesNotMatch(chatView, /<QuestionPrompt[\s\S]{0,300}onStop=/);
  assert.match(
    chatView,
    /onStop=\{pendingPlanReview\.responseState === "recovery_needed"[\s\S]{0,120}\? undefined/,
  );
});

test("acknowledgement labels are localized", () => {
  const zh = JSON.parse(readFileSync("src/i18n/locales/zh/chat.json", "utf8"));
  const en = JSON.parse(readFileSync("src/i18n/locales/en/chat.json", "utf8"));

  assert.equal(zh.interactionSubmitting, "正在提交确认…");
  assert.equal(zh.interactionContinuing, "已确认，正在继续任务…");
  assert.equal(en.interactionAnswered, "Answer submitted");
  assert.equal(zh.interactionRecoveryNeeded, "确认已提交，但暂未收到继续执行状态。");
  assert.equal(en.interactionRecoverAction, "Reconnect");
});

test("empty question events show a recovery action instead of a blank answer field", () => {
  const question = readFileSync(
    "src/components/interactive/question-prompt.tsx",
    "utf8",
  );
  const registry = readFileSync("src/lib/session-stream-registry.ts", "utf8");
  const zh = JSON.parse(readFileSync("src/i18n/locales/zh/chat.json", "utf8"));
  const en = JSON.parse(readFileSync("src/i18n/locales/en/chat.json", "utf8"));

  assert.match(question, /questionText \|\| t\("agentQuestionMissing"\)/);
  assert.match(question, /onRespond\(t\("agentQuestionRetryAnswer"\)\)/);
  assert.match(question, /normalizeQuestionItems/);
  assert.doesNotMatch(question, /\|\|\s*t\("agentQuestion"\)/);
  assert.match(registry, /const questionArguments:[\s\S]*data\.question/);
  assert.equal(zh.agentQuestionRequestAgain, "让 AI 重新提问");
  assert.ok(en.agentQuestionMissing);
});
