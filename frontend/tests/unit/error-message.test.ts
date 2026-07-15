import assert from "node:assert/strict";
import test from "node:test";

import { extractErrorMessage } from "../../src/lib/errors.ts";

test("extractErrorMessage reads structured FastAPI detail messages", () => {
  assert.equal(
    extractErrorMessage(
      {
        detail: {
          code: "goal_budget_exceeds_maximum",
          message: "token_budget exceeds the server maximum of 2000000",
          maximum: 2_000_000,
        },
      },
      "fallback",
    ),
    "token_budget exceeds the server maximum of 2000000",
  );
});
