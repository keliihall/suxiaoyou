import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { test } from "node:test";

const source = readFileSync(
  join(dirname(fileURLToPath(import.meta.url)), "..", "desktop-tauri", "src-tauri", "src", "backend.rs"),
  "utf8",
);

test("desktop backend cold-start contract waits at least 90 seconds", () => {
  const timeoutSeconds = Number(
    source.match(
      /const HEALTH_STARTUP_TIMEOUT: Duration = Duration::from_secs\((\d+)\);/,
    )?.[1],
  );

  assert.ok(
    Number.isFinite(timeoutSeconds),
    "HEALTH_STARTUP_TIMEOUT must remain an explicit absolute deadline",
  );
  assert.ok(
    timeoutSeconds >= 90,
    `desktop production cold-start budget is only ${timeoutSeconds}s`,
  );
});
