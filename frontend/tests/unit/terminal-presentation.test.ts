import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import { isActivityComplete } from "../../src/lib/activity-state.ts";
import { hasVisibleMessageOutput } from "../../src/lib/message-presentation.ts";
import type { ToolPart } from "../../src/types/message.ts";

function tool(
  name: string,
  status: ToolPart["state"]["status"],
  metadata: Record<string, unknown> = {},
): ToolPart {
  return {
    type: "tool",
    tool: name,
    call_id: `${name}-call`,
    state: {
      status,
      input: {},
      output: null,
      metadata,
      title: null,
      time_start: null,
      time_end: null,
      time_compacted: null,
    },
  };
}

test("generated deliverables count as visible assistant output", () => {
  assert.equal(
    hasVisibleMessageOutput([
      tool("bash", "completed", {
        written_files: ["/workspace/suxiaoyou_written/news.mp3"],
      }),
    ]),
    true,
  );
  assert.equal(
    hasVisibleMessageOutput([
      tool("bash", "completed", {
        written_files: ["/workspace/suxiaoyou_written/generate_audio_helper.py"],
      }),
    ]),
    false,
  );
});

test("terminal lifecycle evidence wins over stale running tool state", () => {
  const staleTool = tool("bash", "running");
  assert.equal(
    isActivityComplete({
      toolParts: [staleTool],
      stepParts: [
        { type: "step-finish", reason: "stop", tokens: {}, cost: 0 },
      ],
    }),
    true,
  );
  assert.equal(
    isActivityComplete({
      toolParts: [staleTool],
      stepParts: [],
      isTerminal: true,
    }),
    true,
  );
  assert.equal(
    isActivityComplete({
      toolParts: [staleTool],
      stepParts: [],
      hasVisibleOutput: true,
    }),
    false,
  );
});

test("DONE clears generation before any database reconciliation", () => {
  const registry = readFileSync("src/lib/session-stream-registry.ts", "utf8");
  const start = registry.indexOf("onCurrent(SSE_EVENTS.DONE");
  const end = registry.indexOf("const handleAgentError", start);
  const doneHandler = registry.slice(start, end);

  assert.match(doneHandler, /finishCurrentGeneration\(\)/);
  assert.doesNotMatch(doneHandler, /finishFromDatabase/);
  assert.ok(
    doneHandler.indexOf("finishCurrentGeneration()") <
      doneHandler.indexOf("invalidateQueries"),
  );
});
