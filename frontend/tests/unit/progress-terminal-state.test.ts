import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";


test("finished conversations never render a stale todo as still spinning", () => {
  const source = readFileSync(
    "src/components/workspace/progress-section.tsx",
    "utf8",
  );

  assert.match(
    source,
    /const isActivelyRunning = todo\.status === "in_progress" && isGenerating/,
  );
  assert.match(
    source,
    /wasLeftInProgress \? t\("toolAttemptIncomplete"\) : todo\.activeForm/,
  );
  assert.match(
    source,
    /todo\.status === "in_progress" && !isGenerating[\s\S]*?\? "pending"/,
  );
});
