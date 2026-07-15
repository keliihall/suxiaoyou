import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import {
  EVIDENCE_SCHEMA_VERSION,
  IMAGE_PAID_REQUEST_ACK,
  IntegrationGateError,
  TENCENT_WRITE_ACK,
  buildEvidenceSummary,
  evaluateEvidence,
  evaluateScorecardIntegrationLink,
  preflightTarget,
} from "./v1-real-integration-gates.mjs";


const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const runnerHash = createHash("sha256")
  .update(readFileSync(join(root, "scripts", "v1-real-integration-gates.mjs")))
  .digest("hex");
const commit = "a".repeat(40);
const releaseTag = "v1.0.0-rc.2";
const now = new Date("2026-07-14T12:00:00.000Z");

function imageEnvironment(overrides = {}) {
  return {
    SILICONFLOW_IMAGE_E2E_API_KEY: "sf-live-test-key-value",
    SILICONFLOW_IMAGE_E2E_ALLOW_PAID_REQUEST: IMAGE_PAID_REQUEST_ACK,
    SILICONFLOW_IMAGE_E2E_MAX_REQUESTS: "1",
    SILICONFLOW_IMAGE_E2E_MAX_COST_CNY: "0",
    ...overrides,
  };
}

function tencentEnvironment(overrides = {}) {
  return {
    TENCENT_DOCS_E2E_TOKEN: "tencent-live-token-value",
    TENCENT_DOCS_E2E_ALLOW_WRITE: TENCENT_WRITE_ACK,
    TENCENT_DOCS_E2E_TEST_DOCUMENT_ID: "dedicated-document-id",
    TENCENT_DOCS_E2E_BASELINE_TEXT: "SUYO_E2E_READY",
    TENCENT_DOCS_E2E_READ_ARGS_JSON:
      '{"doc_id":"dedicated-document-id"}',
    TENCENT_DOCS_E2E_WRITE_TOOL: "batch_update_sheet_range",
    TENCENT_DOCS_E2E_WRITE_ARGS_JSON:
      '{"doc_id":"dedicated-document-id","value":"{{SUYO_TENCENT_DOCS_E2E_MARKER}}"}',
    TENCENT_DOCS_E2E_RESTORE_TOOL: "batch_update_sheet_range",
    TENCENT_DOCS_E2E_RESTORE_ARGS_JSON:
      '{"doc_id":"dedicated-document-id","value":"{{SUYO_TENCENT_DOCS_E2E_BASELINE}}"}',
    ...overrides,
  };
}

function record(gateId, target, kind, overrides = {}) {
  const imageLive = gateId === "integration.image_provider.real_e2e";
  const tencentLive = gateId === "integration.tencent_docs.real_e2e";
  return {
    name: `${target}.integration-evidence.json`,
    manifestSha256: "b".repeat(64),
    logHashValid: true,
    manifest: {
      schema_version: EVIDENCE_SCHEMA_VERSION,
      gate_id: gateId,
      target,
      evidence_kind: kind,
      status: "passed",
      evidence_eligible: true,
      tested_at: "2026-07-14T11:00:00.000Z",
      source: {
        commit,
        dirty: false,
        release_tags: [releaseTag],
        stable_during_run: true,
      },
      runner: {
        automatic_retry: false,
        script_sha256: runnerHash,
      },
      preflight: kind === "contract"
        ? { ready: true, credentialFree: true }
        : {
            ready: true,
            credentialPresent: true,
            ...(imageLive ? { automaticRetry: false } : {}),
          },
      contract: {
        real_provider_requests_max: imageLive ? 1 : tencentLive ? 8 : 0,
        restore_attempted_on_ambiguous_failure: tencentLive,
      },
      result: {
        exit_code: 0,
        timed_out: false,
        pytest: { passed: 1, skipped: 0, failed: 0, errors: 0 },
      },
      log: {
        file: `${target}.integration-evidence.log`,
        sha256: "c".repeat(64),
        bytes: 20,
      },
      ...overrides,
    },
  };
}

function contractRecords() {
  return [
    record("integration.tencent_docs.contract", "tencent-contract", "contract"),
    record(
      "integration.image_provider.contract",
      "siliconflow-image-contract",
      "contract",
    ),
  ];
}

test("image preflight requires credential, explicit one-request cap, and cost budget", () => {
  assert.throws(
    () => preflightTarget("siliconflow-image-real", {}, root, now),
    (error) =>
      error instanceof IntegrationGateError &&
      /SILICONFLOW_IMAGE_E2E_API_KEY is required/u.test(error.message),
  );
  assert.throws(
    () =>
      preflightTarget(
        "siliconflow-image-real",
        imageEnvironment({ SILICONFLOW_IMAGE_E2E_MAX_REQUESTS: "2" }),
        root,
        now,
      ),
    /must exactly equal 1/u,
  );

  const result = preflightTarget(
    "siliconflow-image-real",
    imageEnvironment(),
    root,
    now,
  );
  assert.equal(result.ready, true);
  assert.equal(result.maximumProviderRequests, 1);
  assert.equal(result.maximumAcceptedCostCny, 0);
  assert.equal(result.automaticRetry, false);
  assert.equal(result.pricingSourceUrl, "https://siliconflow.cn/pricing");
  assert.doesNotMatch(JSON.stringify(result), /sf-live-test-key-value/u);

  assert.throws(
    () =>
      preflightTarget(
        "siliconflow-image-real",
        imageEnvironment(),
        root,
        new Date("2026-08-20T12:00:00.000Z"),
      ),
    /pricing must be reviewed within 30 days/u,
  );
});

test("Tencent preflight rejects partial or unsafe write/restore configuration", () => {
  assert.throws(
    () =>
      preflightTarget(
        "tencent-real-write",
        tencentEnvironment({ TENCENT_DOCS_E2E_ALLOW_WRITE: "yes" }),
        root,
      ),
    /must exactly equal/u,
  );
  assert.throws(
    () =>
      preflightTarget(
        "tencent-real-write",
        tencentEnvironment({
          TENCENT_DOCS_E2E_RESTORE_TOOL: "delete_space_node",
        }),
        root,
      ),
    /restore tool must be allowlisted and approval-required/u,
  );
  assert.throws(
    () =>
      preflightTarget(
        "tencent-real-write",
        tencentEnvironment({
          TENCENT_DOCS_E2E_WRITE_ARGS_JSON:
            '{"doc_id":"someone-elses-document","value":"{{SUYO_TENCENT_DOCS_E2E_MARKER}}"}',
        }),
        root,
      ),
    /must reference TENCENT_DOCS_E2E_TEST_DOCUMENT_ID/u,
  );
});

test("Tencent preflight proves dedicated-fixture binding without returning secrets", () => {
  const result = preflightTarget("tencent-real-write", tencentEnvironment(), root);
  assert.deepEqual(result, {
    target: "tencent-real-write",
    ready: true,
    credentialPresent: true,
    dedicatedFixtureBound: true,
    reversibleRestoreConfigured: true,
    toolPolicyValidated: true,
  });
  const output = JSON.stringify(result);
  assert.doesNotMatch(output, /tencent-live-token-value/u);
  assert.doesNotMatch(output, /dedicated-document-id/u);
  assert.doesNotMatch(output, /SUYO_E2E_READY/u);
});

test("RC accepts exactly the two clean, same-tag contract records", () => {
  const result = evaluateEvidence(contractRecords(), {
    mode: "rc",
    releaseTag,
    commit,
    now,
    runnerScriptSha256: runnerHash,
  });
  assert.equal(result.passed, true, result.failures.join("\n"));
});

test("GA additionally requires both real-account closures", () => {
  const missing = evaluateEvidence(contractRecords(), {
    mode: "ga",
    releaseTag: "v1.0.0",
    commit,
    now,
    runnerScriptSha256: runnerHash,
  });
  assert.equal(missing.passed, false);
  assert.ok(
    missing.failures.some((failure) =>
      failure.includes("integration.tencent_docs.real_e2e requires exactly one"),
    ),
  );

  const gaRecords = [
    ...contractRecords().map((item) => ({
      ...item,
      manifest: {
        ...item.manifest,
        source: { ...item.manifest.source, release_tags: ["v1.0.0"] },
      },
    })),
    record(
      "integration.tencent_docs.real_e2e",
      "tencent-real-write",
      "real_e2e",
      {
        source: {
          commit,
          dirty: false,
          release_tags: ["v1.0.0"],
          stable_during_run: true,
        },
      },
    ),
    record(
      "integration.image_provider.real_e2e",
      "siliconflow-image-real",
      "real_e2e",
      {
        source: {
          commit,
          dirty: false,
          release_tags: ["v1.0.0"],
          stable_during_run: true,
        },
      },
    ),
  ];
  const complete = evaluateEvidence(gaRecords, {
    mode: "ga",
    releaseTag: "v1.0.0",
    commit,
    now,
    runnerScriptSha256: runnerHash,
  });
  assert.equal(complete.passed, true, complete.failures.join("\n"));
});

test("evidence fails closed on duplicates, skips, dirtiness, expiry, and tampering", () => {
  const base = contractRecords();
  const duplicate = evaluateEvidence([...base, base[0]], {
    mode: "rc",
    releaseTag,
    commit,
    now,
    runnerScriptSha256: runnerHash,
  });
  assert.equal(duplicate.passed, false);
  assert.ok(duplicate.failures.some((failure) => failure.includes("found 2")));

  const unsafe = contractRecords();
  unsafe[0] = {
    ...unsafe[0],
    logHashValid: false,
    manifest: {
      ...unsafe[0].manifest,
      evidence_eligible: false,
      tested_at: "2026-06-01T00:00:00.000Z",
      source: { ...unsafe[0].manifest.source, dirty: true },
      result: {
        ...unsafe[0].manifest.result,
        pytest: { passed: 0, skipped: 1, failed: 0, errors: 0 },
      },
    },
  };
  const result = evaluateEvidence(unsafe, {
    mode: "rc",
    releaseTag,
    commit,
    now,
    runnerScriptSha256: runnerHash,
  });
  assert.equal(result.passed, false);
  for (const expected of [
    "log digest",
    "release-eligible",
    "dirty worktree",
    "expired",
    "non-skipped pass",
  ]) {
    assert.ok(result.failures.some((failure) => failure.includes(expected)), expected);
  }
});

test("target identity, runner digest, restore recovery, and one-request cap are mandatory", () => {
  const gaRecords = [
    ...contractRecords().map((item) => ({
      ...item,
      manifest: {
        ...item.manifest,
        source: { ...item.manifest.source, release_tags: ["v1.0.0"] },
      },
    })),
    record(
      "integration.tencent_docs.real_e2e",
      "tencent-real-write",
      "real_e2e",
      {
        source: {
          commit,
          dirty: false,
          release_tags: ["v1.0.0"],
          stable_during_run: true,
        },
        runner: { automatic_retry: false, script_sha256: "0".repeat(64) },
        contract: {
          real_provider_requests_max: 8,
          restore_attempted_on_ambiguous_failure: false,
        },
      },
    ),
    record(
      "integration.image_provider.real_e2e",
      "tencent-contract",
      "real_e2e",
      {
        source: {
          commit,
          dirty: false,
          release_tags: ["v1.0.0"],
          stable_during_run: true,
        },
        contract: {
          real_provider_requests_max: 2,
          restore_attempted_on_ambiguous_failure: false,
        },
      },
    ),
  ];
  const result = evaluateEvidence(gaRecords, {
    mode: "ga",
    releaseTag: "v1.0.0",
    commit,
    now,
    runnerScriptSha256: runnerHash,
  });
  assert.equal(result.passed, false);
  for (const expected of [
    "runner digest",
    "restore-on-ambiguous-failure",
    "target does not match gate_id",
    "bounded to one provider request",
  ]) {
    assert.ok(result.failures.some((failure) => failure.includes(expected)), expected);
  }
});

test("summary binds gate manifest hashes to release commit and tag", () => {
  const summary = buildEvidenceSummary(contractRecords(), {
    mode: "rc",
    releaseTag,
    commit,
    now,
    runnerScriptSha256: runnerHash,
  });
  assert.equal(summary.status, "passed");
  assert.equal(summary.release_tag, releaseTag);
  assert.equal(summary.release_commit, commit);
  assert.deepEqual(
    summary.gates.map((gate) => gate.gate_id),
    ["integration.image_provider.contract", "integration.tencent_docs.contract"],
  );
  assert.ok(summary.gates.every((gate) => gate.manifest_sha256 === "b".repeat(64)));
});

test("scorecard companion gate requires exact summary hash, commit, tag, and GA live evidence", () => {
  const summaryHash = "d".repeat(64);
  const scorecard = {
    release_tag: releaseTag,
    release_commit: commit,
    integration_evidence: {
      summary_file: "INTEGRATION-EVIDENCE.json",
      summary_sha256: summaryHash,
      release_tag: releaseTag,
      release_commit: commit,
    },
    integrations: {
      tencent_docs: { contract_test: "passed", real_e2e: "pending_credentials" },
      image_provider: { contract_test: "passed", real_e2e: "pending_credentials" },
    },
  };
  const summary = buildEvidenceSummary(contractRecords(), {
    mode: "rc",
    releaseTag,
    commit,
    now,
    runnerScriptSha256: runnerHash,
  });
  const rc = evaluateScorecardIntegrationLink(scorecard, summary, summaryHash, {
    mode: "rc",
  });
  assert.equal(rc.passed, true, rc.failures.join("\n"));

  const wrongHash = evaluateScorecardIntegrationLink(scorecard, summary, "e".repeat(64), {
    mode: "rc",
  });
  assert.equal(wrongHash.passed, false);
  assert.ok(wrongHash.failures.some((failure) => failure.includes("SHA-256")));

  const ga = evaluateScorecardIntegrationLink(
    {
      ...scorecard,
      release_tag: "v1.0.0",
      integration_evidence: {
        ...scorecard.integration_evidence,
        release_tag: "v1.0.0",
      },
    },
    { ...summary, mode: "ga", release_tag: "v1.0.0" },
    summaryHash,
    { mode: "ga" },
  );
  assert.equal(ga.passed, false);
  assert.ok(ga.failures.some((failure) => failure.includes("real_e2e must be passed")));
  assert.ok(
    ga.failures.some((failure) =>
      failure.includes("integration.tencent_docs.real_e2e"),
    ),
  );
});
