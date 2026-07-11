import assert from "node:assert/strict";
import test from "node:test";

import {
  backendLifecycleReducer,
  INITIAL_DESKTOP_BACKEND_STATE,
} from "../../src/lib/backend-lifecycle.ts";

test("native lifecycle advances through initialization and recovery", () => {
  const ready = backendLifecycleReducer(INITIAL_DESKTOP_BACKEND_STATE, {
    type: "native-status",
    status: { phase: "ready", revision: 1, url: "http://127.0.0.1:4100" },
  });
  assert.equal(ready.status.phase, "ready");

  const restarting = backendLifecycleReducer(ready, {
    type: "native-status",
    status: { phase: "restarting", revision: 2, attempt: 1, max_attempts: 3 },
  });
  assert.equal(restarting.status.phase, "restarting");
  assert.equal(restarting.status.attempt, 1);

  const recovered = backendLifecycleReducer(restarting, {
    type: "native-status",
    status: { phase: "ready", revision: 3, url: "http://127.0.0.1:4200" },
  });
  assert.equal(recovered.status.phase, "ready");
  assert.equal(recovered.status.url, "http://127.0.0.1:4200");
});

test("terminal native failure remains visible and supports relaunch feedback", () => {
  const failed = backendLifecycleReducer(INITIAL_DESKTOP_BACKEND_STATE, {
    type: "native-status",
    status: {
      phase: "failed",
      revision: 4,
      failure_code: "backend_start_failed",
      detail: "process exited before health check",
    },
  });
  assert.equal(failed.status.phase, "failed");
  assert.equal(failed.status.failure_code, "backend_start_failed");

  const relaunching = backendLifecycleReducer(failed, {
    type: "relaunch-requested",
  });
  assert.equal(relaunching.relaunching, true);

  const relaunchFailed = backendLifecycleReducer(relaunching, {
    type: "action-failed",
    detail: "relaunch unavailable",
  });
  assert.equal(relaunchFailed.status.phase, "failed");
  assert.equal(relaunchFailed.relaunching, false);
  assert.equal(relaunchFailed.actionError, "relaunch unavailable");
});

test("stale native snapshots cannot overwrite newer events", () => {
  const current = backendLifecycleReducer(INITIAL_DESKTOP_BACKEND_STATE, {
    type: "native-status",
    status: { phase: "ready", revision: 9, url: "http://127.0.0.1:4300" },
  });
  const stale = backendLifecycleReducer(current, {
    type: "native-status",
    status: { phase: "initializing", revision: 8 },
  });
  assert.strictEqual(stale, current);
});

test("duplicate native revisions cannot clear in-flight recovery feedback", () => {
  const failed = backendLifecycleReducer(INITIAL_DESKTOP_BACKEND_STATE, {
    type: "native-status",
    status: {
      phase: "failed",
      revision: 9,
      failure_code: "backend_start_failed",
    },
  });
  const relaunching = backendLifecycleReducer(failed, {
    type: "relaunch-requested",
  });
  const duplicate = backendLifecycleReducer(relaunching, {
    type: "native-status",
    status: {
      phase: "failed",
      revision: 9,
      failure_code: "backend_start_failed",
    },
  });

  assert.strictEqual(duplicate, relaunching);
  assert.equal(duplicate.relaunching, true);
});

test("UI action and provider failures cannot change the native phase", () => {
  const ready = backendLifecycleReducer(INITIAL_DESKTOP_BACKEND_STATE, {
    type: "native-status",
    status: { phase: "ready", revision: 2, url: "http://127.0.0.1:4400" },
  });

  const ignoredRelaunch = backendLifecycleReducer(ready, {
    type: "relaunch-requested",
  });
  assert.strictEqual(ignoredRelaunch, ready);

  const actionFailed = backendLifecycleReducer(ready, {
    type: "action-failed",
    detail: "provider request failed",
  });
  assert.equal(actionFailed.status.phase, "ready");
  assert.equal(actionFailed.status.revision, 2);
});

test("a lifecycle bridge failure produces a visible local failure", () => {
  const state = backendLifecycleReducer(INITIAL_DESKTOP_BACKEND_STATE, {
    type: "bootstrap-failed",
    detail: "IPC unavailable",
  });
  assert.equal(state.status.phase, "failed");
  assert.equal(state.status.failure_code, "lifecycle_unavailable");
  assert.equal(state.status.detail, "IPC unavailable");
});

test("a late snapshot invoke error cannot overwrite a native event", () => {
  const ready = backendLifecycleReducer(INITIAL_DESKTOP_BACKEND_STATE, {
    type: "native-status",
    status: { phase: "ready", revision: 3, url: "http://127.0.0.1:4500" },
  });
  const afterInvokeError = backendLifecycleReducer(ready, {
    type: "bootstrap-failed",
    detail: "snapshot invoke failed",
  });
  assert.strictEqual(afterInvokeError, ready);
});
