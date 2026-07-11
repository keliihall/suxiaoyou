import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  canCommitRemoteStreamAttach,
  needsRemoteStreamAttach,
  StreamLeaseRegistry,
} from "../../src/lib/stream-lifecycle.ts";

test("a newer stream wins when two starts resolve out of order", async () => {
  const leases = new StreamLeaseRegistry();
  const attached: string[] = [];
  let releaseOld!: () => void;
  let releaseNew!: () => void;
  const oldSetup = new Promise<void>((resolve) => { releaseOld = resolve; });
  const newSetup = new Promise<void>((resolve) => { releaseNew = resolve; });

  const start = async (streamId: string, setup: Promise<void>) => {
    const lease = leases.expect("session", streamId);
    await setup;
    if (leases.isCurrent(lease)) attached.push(streamId);
  };

  const oldStart = start("old-stream", oldSetup);
  const newStart = start("new-stream", newSetup);
  releaseNew();
  await newStart;
  releaseOld();
  await oldStart;

  assert.deepEqual(attached, ["new-stream"]);
  assert.equal(leases.current("session")?.streamId, "new-stream");
});

test("late cleanup from an old stream cannot clear the new expectation", () => {
  const leases = new StreamLeaseRegistry();
  const oldLease = leases.expect("session", "old-stream");
  const newLease = leases.expect("session", "new-stream");

  assert.equal(leases.clear("session", oldLease), false);
  assert.equal(leases.isCurrent(newLease), true);
  assert.equal(leases.clear("session", newLease), true);
  assert.equal(leases.current("session"), undefined);
});

test("remote sync replaces an attached old stream with the backend's new stream", () => {
  assert.equal(needsRemoteStreamAttach("new-stream", "old-stream"), true);
  assert.equal(needsRemoteStreamAttach("new-stream", "new-stream"), false);
  assert.equal(needsRemoteStreamAttach("new-stream", null), true);

  const hook = readFileSync("src/hooks/use-remote-generation-sync.ts", "utf8");
  assert.match(hook, /getActiveStreamId\(sessionId\)/);
  assert.doesNotMatch(hook, /isStreamActive/);
  assert.match(
    hook,
    /await startStream\(sessionId, match\.stream_id\)[\s\S]*getActiveStreamId\(sessionId\) === match\.stream_id[\s\S]*knownStreamIdRef\.current = match\.stream_id/,
  );
});

test("a deferred invalidation cannot attach a poll result over a newer local stream", async () => {
  const before = {
    registryStreamId: "old-stream",
    registryGeneration: 3,
    bucketStreamId: "old-stream",
    bucketGenerationStartedAt: 100,
  };
  let current = before;
  let releaseInvalidation!: () => void;
  const invalidation = new Promise<void>((resolve) => {
    releaseInvalidation = resolve;
  });

  const commitAfterInvalidation = (async () => {
    await invalidation;
    return canCommitRemoteStreamAttach({
      pollSequence: 4,
      currentPollSequence: 4,
      expectedBackendStreamId: "old-stream",
      confirmedBackendStreamId: "old-stream",
      before,
      after: current,
    });
  })();

  current = {
    registryStreamId: "new-stream",
    registryGeneration: 5,
    bucketStreamId: "new-stream",
    bucketGenerationStartedAt: 200,
  };
  releaseInvalidation();
  assert.equal(await commitAfterInvalidation, false);

  assert.equal(canCommitRemoteStreamAttach({
    pollSequence: 4,
    currentPollSequence: 5,
    expectedBackendStreamId: "old-stream",
    confirmedBackendStreamId: "old-stream",
    before,
    after: before,
  }), false);
});

test("replacing a stream invalidates the old buffer before disposal", () => {
  const source = readFileSync("src/lib/session-stream-registry.ts", "utf8");
  const leaseIndex = source.indexOf("const lease = streamLeases.expect(sessionId, streamId)");
  const disposeIndex = source.indexOf("disposeInstance(existing)", leaseIndex);

  assert.ok(leaseIndex >= 0 && disposeIndex > leaseIndex);
  assert.match(source, /bucket\.streamId === instance\.streamId/);
  assert.match(source, /if \(isCurrentGeneration\(\)\) store\.getState\(\)\.appendTextDelta/);
});

test("registry gates setup and disconnect recovery instead of forcing completion", () => {
  const source = readFileSync("src/lib/session-stream-registry.ts", "utf8");

  assert.match(source, /const streamLeases = new StreamLeaseRegistry\(\)/);
  assert.match(source, /await Promise\.all[\s\S]*if \(!streamLeases\.isCurrent\(lease\)\) return/);
  assert.doesNotMatch(source, /pendingStarts/);
  assert.match(source, /status === "disconnected"[\s\S]*recoverDisconnectedStream/);
  assert.doesNotMatch(
    source,
    /status === "disconnected"[\s\S]{0,700}finally\s*\{[\s\S]{0,200}finishCurrentGeneration/,
  );
});
