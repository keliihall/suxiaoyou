import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const source = readFileSync("src/components/chat/chat-form.tsx", "utf8");

function handleSendSource(): string {
  const start = source.indexOf("const handleSend = useCallback");
  const end = source.indexOf("const handleSendTaskBatch", start);
  assert.notEqual(start, -1, "ChatForm handleSend was not found");
  assert.notEqual(end, -1, "ChatForm handleSend boundary was not found");
  return source.slice(start, end);
}

function position(haystack: string, needle: string): number {
  const index = haystack.indexOf(needle);
  assert.notEqual(index, -1, `missing source contract: ${needle}`);
  return index;
}

test("ChatForm exposes an optional typed Goal command dispatcher", () => {
  assert.match(
    source,
    /onGoalCommand\?: \([\s\S]*command: ParsedGoalCommand,[\s\S]*attachments\?: FileAttachment\[\],[\s\S]*\) => Promise<boolean \| void> \| boolean \| void/,
  );
  assert.match(source, /parseGoalCommand, type ParsedGoalCommand/);
});

test("Goal commands are resolved before ordinary send and queue dispatch", () => {
  const handler = handleSendSource();
  const parse = position(handler, "const goalCommand = parseGoalCommand(input);");
  const goalBranch = position(handler, "if (goalCommand) {");
  const ordinaryGuard = position(handler, "if (isGenerating && !onQueue) return;");
  const ordinaryQueue = position(handler, "await onQueue(");
  const ordinarySend = position(handler, "await onSend(");

  assert.ok(parse < goalBranch);
  assert.ok(goalBranch < ordinaryGuard);
  assert.ok(ordinaryGuard < ordinaryQueue);
  assert.ok(ordinaryGuard < ordinarySend);

  const goalOnlyPath = handler.slice(goalBranch, ordinaryGuard);
  assert.doesNotMatch(goalOnlyPath, /\bonSend\s*\(/);
  assert.doesNotMatch(goalOnlyPath, /\bonQueue\s*\(/);
});

test("Goal validation retains the draft and only clears an accepted command", () => {
  const handler = handleSendSource();
  const goalBranch = position(handler, "if (goalCommand) {");
  const ordinaryGuard = position(handler, "if (isGenerating && !onQueue) return;");
  const goalOnlyPath = handler.slice(goalBranch, ordinaryGuard);

  for (const contract of [
    "if (!goalCommand.ok)",
    't("goalCommandObjectiveRequired")',
    't("goalCommandObjectiveTooLong"',
    't("goalCommandUnexpectedArgument")',
    "if (!onGoalCommand)",
    't("goalCommandUnavailable")',
  ]) {
    position(goalOnlyPath, contract);
  }

  assert.doesNotMatch(goalOnlyPath, /if \(attachments\.length > 0\)/);
  const dispatch = position(goalOnlyPath, "const result = await onGoalCommand(");
  const attachmentForwarding = position(
    goalOnlyPath,
    "submittedAttachments.length > 0 ? submittedAttachments : undefined",
  );
  const rejected = position(goalOnlyPath, "if (result === false) return;");
  const unchangedDraft = position(
    goalOnlyPath,
    "const inputUnchanged = inputRef.current === submittedText",
  );
  const clear = position(goalOnlyPath, 'setInput("");');
  const clearAttachments = position(goalOnlyPath, "setAttachments([]);");
  assert.ok(dispatch < attachmentForwarding);
  assert.ok(attachmentForwarding < rejected);
  assert.ok(rejected < unchangedDraft);
  assert.ok(unchangedDraft < clear);
  assert.ok(clear < clearAttachments);
  assert.match(goalOnlyPath, /catch \(error\)[\s\S]*goalCommandFailed/);
});

test("Goal command validation messages are localized", () => {
  const zh = JSON.parse(readFileSync("src/i18n/locales/zh/chat.json", "utf8"));
  const en = JSON.parse(readFileSync("src/i18n/locales/en/chat.json", "utf8"));

  for (const key of [
    "goalCommandObjectiveRequired",
    "goalCommandObjectiveTooLong",
    "goalCommandUnexpectedArgument",
    "goalCommandAttachmentsUnsupported",
    "goalCommandUnavailable",
    "goalCommandFailed",
  ]) {
    assert.equal(typeof zh[key], "string", `missing zh key ${key}`);
    assert.equal(typeof en[key], "string", `missing en key ${key}`);
    assert.ok(zh[key].length > 0, `empty zh key ${key}`);
    assert.ok(en[key].length > 0, `empty en key ${key}`);
  }
});
