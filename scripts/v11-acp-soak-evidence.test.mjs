import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { spawnSync } from "node:child_process";
import {
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  symlinkSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import {
  ACP_SOAK_CONTRACT_VERSION,
  AcpSoakEvidenceError,
  REQUIRED_ACP_SOAK_COVERAGE,
  aggregateAcpSoakEvidence,
  validateAcpSoakReport,
} from "./v11-acp-soak-evidence.mjs";

const COMMIT = "a".repeat(40);
const RELEASE_REF = "v1.1.0-rc.3";
const AGENT_BINARY_IDENTITY_SHA256 = createHash("sha256")
  .update("v1.1 frozen backend matrix")
  .digest("hex");
const SCRIPT = fileURLToPath(new URL("./v11-acp-soak-evidence.mjs", import.meta.url));

function hash(character) {
  return character.repeat(64);
}

function passingReport(clientId, index = 0) {
  return {
    schema_version: 1,
    contract_version: ACP_SOAK_CONTRACT_VERSION,
    status: "ok",
    run_id: `soak-${clientId}-${index}`,
    source_commit: COMMIT,
    release_ref: RELEASE_REF,
    platform: index % 2 === 0 ? "macos-arm64" : "linux-x64",
    started_at: `2026-07-${String(18 + index).padStart(2, "0")}T00:00:00.000Z`,
    completed_at: `2026-07-${String(18 + index).padStart(2, "0")}T08:00:00.000Z`,
    client: {
      id: clientId,
      name: `ACP Client ${index + 1}`,
      version: `1.0.${index}`,
      executable_sha256: hash(String(index + 1)),
      provenance_sha256: hash(String(index + 3)),
    },
    agent: {
      binary_sha256: hash(String(index + 5)),
      binary_identity_manifest_sha256: AGENT_BINARY_IDENTITY_SHA256,
      frozen_backend: true,
      protocol_version: 1,
      sdk_name: "agent-client-protocol",
      sdk_version: "0.10.1",
    },
    coverage: Object.fromEntries(
      REQUIRED_ACP_SOAK_COVERAGE.map((field) => [field, true]),
    ),
    counts: {
      sessions_created: 10,
      sessions_loaded: 10,
      prompts_completed: 100,
      permission_requests: 20,
      cancellations: 20,
      disconnects: 20,
      cjk_workspace_runs: 20,
      agent_uptime_seconds: 28_800,
      duplicate_writes: 0,
      unfinished_journals: 0,
      orphan_processes: 0,
      cross_session_events: 0,
      protocol_errors: 0,
    },
    privacy: {
      prompts_collected: false,
      file_paths_collected: false,
      secrets_collected: false,
    },
  };
}

function inputs(reports = [passingReport("client-alpha", 0), passingReport("client-beta", 1)]) {
  return reports.map((value, index) => ({
    path: `client-${index + 1}/acp-soak-report.json`,
    value,
    raw: `${JSON.stringify(value)}\n`,
  }));
}

function aggregateBindings() {
  return {
    expectedCommit: COMMIT,
    expectedReleaseRef: RELEASE_REF,
    expectedAgentBinaryIdentitySha256: AGENT_BINARY_IDENTITY_SHA256,
  };
}

test("accepts two distinct release-bound eight-hour ACP client soaks", () => {
  const summary = aggregateAcpSoakEvidence(inputs(), aggregateBindings());
  assert.equal(summary.status, "ok");
  assert.equal(summary.client_count, 2);
  assert.equal(summary.minimum_duration_milliseconds, 8 * 60 * 60 * 1000);
  assert.deepEqual(
    summary.clients.map((item) => item.client.id),
    ["client-alpha", "client-beta"],
  );
  for (const item of summary.clients) {
    assert.match(item.report_sha256, /^[0-9a-f]{64}$/u);
    assert.equal(item.counts.orphan_processes, 0);
    assert.equal(item.privacy.file_paths_collected, false);
  }
});

test("validates the exact raw bytes that receive the published digest", () => {
  const reports = inputs();
  reports[0].raw = `${JSON.stringify({ ...reports[0].value, status: "failed" })}\n`;
  assert.throws(
    () =>
      aggregateAcpSoakEvidence(reports, aggregateBindings()),
    /status must be ok/u,
  );

  const valid = inputs();
  const summary = aggregateAcpSoakEvidence(valid, aggregateBindings());
  assert.equal(
    summary.clients[0].report_sha256,
    createHash("sha256").update(valid[0].raw).digest("hex"),
  );
});

test("requires canonical original bytes and the release backend identity", () => {
  const reports = inputs();
  assert.throws(
    () =>
      aggregateAcpSoakEvidence(
        [{ value: reports[0].value }, reports[1]],
        aggregateBindings(),
      ),
    /original report bytes/u,
  );

  const pretty = inputs();
  pretty[0].raw = `${JSON.stringify(pretty[0].value, null, 2)}\n`;
  assert.throws(
    () => aggregateAcpSoakEvidence(pretty, aggregateBindings()),
    /canonical single-line JSON bytes/u,
  );

  const wrongIdentity = inputs();
  wrongIdentity[0].value.agent.binary_identity_manifest_sha256 = hash("8");
  wrongIdentity[0].raw = `${JSON.stringify(wrongIdentity[0].value)}\n`;
  assert.throws(
    () => aggregateAcpSoakEvidence(wrongIdentity, aggregateBindings()),
    /release matrix/u,
  );
});

test("rejects one client and duplicate client implementation identities", () => {
  assert.throws(
    () =>
      aggregateAcpSoakEvidence(inputs().slice(0, 1), aggregateBindings()),
    /exactly two/u,
  );

  const duplicate = passingReport("client-alpha", 1);
  assert.throws(
    () =>
      aggregateAcpSoakEvidence(
        inputs([passingReport("client-alpha", 0), duplicate]),
        aggregateBindings(),
      ),
    /distinct client implementations/u,
  );

  const sameBuild = passingReport("client-beta", 1);
  sameBuild.client.executable_sha256 = passingReport("client-alpha", 0).client.executable_sha256;
  assert.throws(
    () =>
      aggregateAcpSoakEvidence(
        inputs([passingReport("client-alpha", 0), sameBuild]),
        aggregateBindings(),
      ),
    /distinct client implementations/u,
  );
});

test("rejects short runs, missing scenarios, nonzero safety findings, and private fields", () => {
  const cases = [];
  const short = passingReport("client-alpha");
  short.completed_at = "2026-07-18T07:59:59.999Z";
  cases.push([short, /at least 8 hours/u]);
  const missingCoverage = passingReport("client-alpha");
  missingCoverage.coverage.cancel = false;
  cases.push([missingCoverage, /coverage\.cancel must be true/u]);
  const orphan = passingReport("client-alpha");
  orphan.counts.orphan_processes = 1;
  cases.push([orphan, /orphan_processes must be zero/u]);
  const privateField = passingReport("client-alpha");
  privateField.prompt = "must never enter release evidence";
  cases.push([privateField, /fields must be exactly/u]);

  for (const [report, pattern] of cases) {
    assert.throws(
      () =>
        validateAcpSoakReport(report, {
          expectedCommit: COMMIT,
          expectedReleaseRef: RELEASE_REF,
        }),
      pattern,
    );
  }
});

test("rejects release mismatch, source-only agents, and protocol drift", () => {
  const mismatch = passingReport("client-alpha");
  mismatch.source_commit = "b".repeat(40);
  assert.throws(
    () =>
      validateAcpSoakReport(mismatch, {
        expectedCommit: COMMIT,
        expectedReleaseRef: RELEASE_REF,
      }),
    /release commit/u,
  );

  const sourceOnly = passingReport("client-alpha");
  sourceOnly.agent.frozen_backend = false;
  assert.throws(() => validateAcpSoakReport(sourceOnly), /frozen_backend/u);

  const protocolDrift = passingReport("client-alpha");
  protocolDrift.agent.protocol_version = 2;
  assert.throws(() => validateAcpSoakReport(protocolDrift), /protocol_version/u);

  const sdkDrift = passingReport("client-alpha");
  sdkDrift.agent.sdk_version = "0.10.2";
  assert.throws(() => validateAcpSoakReport(sdkDrift), /sdk_version/u);

  const falseUptime = passingReport("client-alpha");
  falseUptime.completed_at = "2026-07-18T08:00:01.000Z";
  assert.throws(() => validateAcpSoakReport(falseUptime), /uptime_seconds/u);
});

test("CLI discovers exactly two regular reports and writes a deterministic summary", (t) => {
  const root = mkdtempSync(join(tmpdir(), "suyo-v11-acp-soak-"));
  t.after(() => rmSync(root, { recursive: true, force: true }));
  for (const [index, report] of [passingReport("client-alpha", 0), passingReport("client-beta", 1)].entries()) {
    const directory = join(root, `client-${index + 1}`);
    mkdirSync(directory);
    writeFileSync(
      join(directory, "acp-soak-report.json"),
      `${JSON.stringify(report)}\n`,
    );
  }
  const output = join(root, "summary", "ACP-SOAK-EVIDENCE.json");
  const result = spawnSync(
    process.execPath,
    [
      SCRIPT,
      "aggregate",
      root,
      COMMIT,
      RELEASE_REF,
      AGENT_BINARY_IDENTITY_SHA256,
      output,
    ],
    { encoding: "utf8" },
  );
  assert.equal(result.status, 0, result.stderr);
  assert.match(result.stdout, /verified 2 clients/u);
  const summary = JSON.parse(readFileSync(output, "utf8"));
  assert.equal(summary.client_count, 2);
  assert.equal(summary.source_commit, COMMIT);

  const overwrite = spawnSync(
    process.execPath,
    [
      SCRIPT,
      "aggregate",
      root,
      COMMIT,
      RELEASE_REF,
      AGENT_BINARY_IDENTITY_SHA256,
      output,
    ],
    { encoding: "utf8" },
  );
  assert.notEqual(overwrite.status, 0);
  assert.match(overwrite.stderr, /already exists/u);
});

test("CLI rejects a symlinked client report", (t) => {
  const root = mkdtempSync(join(tmpdir(), "suyo-v11-acp-soak-link-"));
  t.after(() => rmSync(root, { recursive: true, force: true }));
  const real = join(root, "real.json");
  writeFileSync(real, JSON.stringify(passingReport("client-alpha")));
  const directory = join(root, "client");
  mkdirSync(directory);
  symlinkSync(real, join(directory, "acp-soak-report.json"));
  const result = spawnSync(
    process.execPath,
    [
      SCRIPT,
      "aggregate",
      root,
      COMMIT,
      RELEASE_REF,
      AGENT_BINARY_IDENTITY_SHA256,
      join(root, "out.json"),
    ],
    { encoding: "utf8" },
  );
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /must not contain symbolic links/u);
});

test("public validation errors remain content-free", () => {
  assert.throws(
    () => validateAcpSoakReport({ secret: "do-not-leak" }),
    AcpSoakEvidenceError,
  );
});
