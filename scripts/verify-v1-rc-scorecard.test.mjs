import assert from "node:assert/strict";
import test from "node:test";

import {
  REQUIRED_BETA_WORKFLOWS,
  REQUIRED_OFFICE_PLATFORMS,
  REQUIRED_PACKAGE_KINDS,
  evaluateV1RcScorecard,
} from "./verify-v1-rc-scorecard.mjs";

function passingScorecard() {
  const workflow = {
    total: 40,
    succeeded: 38,
    output_tasks: 20,
    output_revalidated: 20,
    unrecoverable_data_loss: 0,
  };
  const officeResult = {
    created: true,
    edited: true,
    reopened_and_validated: true,
    independent_reopen_validated: true,
    atomic_install: true,
    version_snapshot_verified: true,
    initial_sha256: "1".repeat(64),
    final_sha256: "2".repeat(64),
    final_size: 1024,
    previous_version_id: "01OFFICECONTRACTVERSION0000",
  };
  return {
    schema_version: 1,
    release_tag: "v1.0.0-rc.1",
    release_commit: "a".repeat(40),
    integration_evidence: {
      summary_file: "INTEGRATION-CONTRACTS.json",
      summary_sha256: "e".repeat(64),
      release_tag: "v1.0.0-rc.1",
      release_commit: "a".repeat(40),
    },
    beta_evidence: {
      summary_sha256: "f".repeat(64),
      source_sha256: "9".repeat(64),
      release_tag: "v1.0.0-rc.1",
      release_commit: "a".repeat(40),
    },
    packages: REQUIRED_PACKAGE_KINDS.map((kind) => ({
      kind,
      tag: "v1.0.0-rc.1",
      source_commit: "a".repeat(40),
      artifact_sha256: "c".repeat(64),
      lifecycle_report_sha256: "d".repeat(64),
      executable_path: `/opt/suyo/${kind}`,
      executable_size: 4096,
      executable_sha256: "8".repeat(64),
      checksum_verified: true,
      installed: true,
      launched: true,
      exited_cleanly: true,
      no_orphan_processes: true,
      ...(kind.startsWith("macos-")
        ? { artifact_profile: "rc-adhoc", trust_boundary_verified: true }
        : {}),
    })),
    office_compatibility: REQUIRED_OFFICE_PLATFORMS.map((platform) => ({
      platform,
      contract_version: "v1.0-restricted-office-1",
      source_commit: "a".repeat(40),
      release_ref: "v1.0.0-rc.1",
      report_sha256: "b".repeat(64),
      runner: { frozen_backend: true },
      docx: { ...officeResult },
      xlsx: { ...officeResult },
      pptx: { ...officeResult },
    })),
    beta: {
      controlled_user_count: 20,
      started_at: "2026-07-01T00:00:00Z",
      ended_at: "2026-07-08T00:00:00Z",
      workflows: Object.fromEntries(
        REQUIRED_BETA_WORKFLOWS.map((name) => [name, { ...workflow }]),
      ),
    },
    quality: {
      open_p0: 0,
      open_p1: 0,
      backend_full_suite_passed: true,
      frontend_full_suite_passed: true,
      playwright_core_passed: true,
      rust_full_suite_passed: true,
      migrations_passed: true,
      supply_chain_vulnerabilities_zero: true,
      security_boundary_regression_passed: true,
    },
    integrations: {
      tencent_docs: { contract_test: "passed", real_e2e: "pending_credentials" },
      image_provider: { contract_test: "passed", real_e2e: "pending_credentials" },
    },
  };
}

test("complete RC evidence passes every machine-readable gate", () => {
  const result = evaluateV1RcScorecard(passingScorecard());
  assert.equal(result.ok, true);
  assert.equal(result.failures.length, 0);
});

test("beta task quality, data loss, and package omissions fail closed", () => {
  const scorecard = passingScorecard();
  scorecard.beta.workflows.file_organization.succeeded = 0;
  scorecard.beta.workflows.office_create_edit.unrecoverable_data_loss = 1;
  scorecard.packages = scorecard.packages.filter((item) => item.kind !== "linux-arm64-rpm");
  const result = evaluateV1RcScorecard(scorecard);
  const ids = new Set(result.failures.map((gate) => gate.id));
  assert.equal(result.ok, false);
  assert.ok(ids.has("beta.task_success_rate"));
  assert.ok(ids.has("beta.unrecoverable_data_loss"));
  assert.ok(ids.has("package.linux-arm64-rpm.present"));
});

test("GA additionally requires stable tag, real E2E, Developer ID, and notarization", () => {
  const scorecard = passingScorecard();
  const first = evaluateV1RcScorecard(scorecard, { ga: true });
  assert.equal(first.ok, false);
  assert.ok(first.failures.some((gate) => gate.id.endsWith("developer_id_signed")));
  assert.ok(first.failures.some((gate) => gate.id === "integration.tencent_docs.real_e2e"));

  scorecard.release_tag = "v1.0.0";
  scorecard.integration_evidence.release_tag = "v1.0.0";
  for (const item of scorecard.office_compatibility) item.release_ref = "v1.0.0";
  for (const item of scorecard.packages) {
    item.tag = "v1.0.0";
    if (item.kind.startsWith("macos-")) {
      item.developer_id_signed = true;
      item.notarized = true;
    }
  }
  scorecard.integrations.tencent_docs.real_e2e = "passed";
  scorecard.integrations.image_provider.real_e2e = "passed";
  assert.equal(evaluateV1RcScorecard(scorecard, { ga: true }).ok, true);
});

test("invalid or short Beta intervals cannot satisfy the seven-day gate", () => {
  const scorecard = passingScorecard();
  scorecard.beta.ended_at = "2026-07-07T23:59:59Z";
  let result = evaluateV1RcScorecard(scorecard);
  assert.ok(result.failures.some((gate) => gate.id === "beta.duration"));

  scorecard.beta.ended_at = "not-a-date";
  result = evaluateV1RcScorecard(scorecard);
  assert.ok(result.failures.some((gate) => gate.id === "beta.duration"));
});

test("missing zero-valued safety evidence and duplicate package rows fail closed", () => {
  const scorecard = passingScorecard();
  delete scorecard.quality.open_p0;
  delete scorecard.beta.workflows.connector_read.unrecoverable_data_loss;
  scorecard.packages.push({ ...scorecard.packages[0] });
  const result = evaluateV1RcScorecard(scorecard);
  const ids = new Set(result.failures.map((gate) => gate.id));
  assert.ok(ids.has("quality.open_p0"));
  assert.ok(ids.has("beta.workflow.connector_read.counts_valid"));
  assert.ok(ids.has(`package.${scorecard.packages[0].kind}.present`));
});

test("GA rejects an RC tag even when all other stable gates pass", () => {
  const scorecard = passingScorecard();
  for (const item of scorecard.packages) {
    if (item.kind.startsWith("macos-")) {
      item.developer_id_signed = true;
      item.notarized = true;
    }
  }
  scorecard.integrations.tencent_docs.real_e2e = "passed";
  scorecard.integrations.image_provider.real_e2e = "passed";
  const result = evaluateV1RcScorecard(scorecard, { ga: true });
  assert.ok(result.failures.some((gate) => gate.id === "release.tag"));
});

test("Office evidence must come from the frozen backend at the release commit", () => {
  const scorecard = passingScorecard();
  scorecard.office_compatibility[0].runner.frozen_backend = false;
  scorecard.office_compatibility[1].source_commit = "c".repeat(40);
  scorecard.office_compatibility[2].pptx.independent_reopen_validated = false;
  delete scorecard.office_compatibility[3].report_sha256;
  const result = evaluateV1RcScorecard(scorecard);
  const ids = new Set(result.failures.map((gate) => gate.id));
  assert.ok(ids.has(`office.${REQUIRED_OFFICE_PLATFORMS[0]}.frozen_backend`));
  assert.ok(ids.has(`office.${REQUIRED_OFFICE_PLATFORMS[1]}.same_commit`));
  assert.ok(
    ids.has(`office.${REQUIRED_OFFICE_PLATFORMS[2]}.pptx.independent_reopen_validated`),
  );
  assert.ok(ids.has(`office.${REQUIRED_OFFICE_PLATFORMS[3]}.report_sha256`));
});

test("package provenance, exact record sets, and RC E2E states fail closed", () => {
  const scorecard = passingScorecard();
  scorecard.packages[0].source_commit = "e".repeat(40);
  delete scorecard.packages[1].artifact_sha256;
  scorecard.packages.push({ kind: "unexpected-installer" });
  scorecard.office_compatibility.push({ platform: "freebsd-x64" });
  scorecard.integrations.tencent_docs.real_e2e = "not_run";
  const result = evaluateV1RcScorecard(scorecard);
  const ids = new Set(result.failures.map((gate) => gate.id));
  assert.ok(ids.has("package.records_exact"));
  assert.ok(ids.has(`package.${REQUIRED_PACKAGE_KINDS[0]}.same_commit`));
  assert.ok(ids.has(`package.${REQUIRED_PACKAGE_KINDS[1]}.artifact_sha256`));
  assert.ok(ids.has("office.records_exact"));
  assert.ok(ids.has("integration.tencent_docs.real_e2e"));
});

test("package evidence identifies the exact executable used by lifecycle smoke", () => {
  const scorecard = passingScorecard();
  delete scorecard.packages[0].executable_sha256;
  scorecard.packages[1].executable_size = 0;
  scorecard.packages[2].executable_path = "";
  const result = evaluateV1RcScorecard(scorecard);
  const ids = new Set(result.failures.map((gate) => gate.id));
  assert.ok(ids.has(`package.${REQUIRED_PACKAGE_KINDS[0]}.executable_sha256`));
  assert.ok(ids.has(`package.${REQUIRED_PACKAGE_KINDS[1]}.executable_size`));
  assert.ok(ids.has(`package.${REQUIRED_PACKAGE_KINDS[2]}.executable_path`));
});

test("the primary scorecard cannot ignore or detach integration evidence", () => {
  const scorecard = passingScorecard();
  delete scorecard.integration_evidence.summary_sha256;
  scorecard.integration_evidence.release_tag = "v1.0.0-rc.2";
  scorecard.integration_evidence.release_commit = "b".repeat(40);
  scorecard.integration_evidence.summary_file = "some-other-summary.json";
  const result = evaluateV1RcScorecard(scorecard);
  const ids = new Set(result.failures.map((gate) => gate.id));
  assert.ok(ids.has("integration_evidence.summary_file"));
  assert.ok(ids.has("integration_evidence.summary_sha256"));
  assert.ok(ids.has("integration_evidence.same_tag"));
  assert.ok(ids.has("integration_evidence.same_commit"));
});

test("the primary scorecard requires release-bound Beta summary and source digests", () => {
  const scorecard = passingScorecard();
  delete scorecard.beta_evidence.summary_sha256;
  scorecard.beta_evidence.source_sha256 = "not-a-digest";
  scorecard.beta_evidence.release_tag = "v1.0.0-rc.2";
  scorecard.beta_evidence.release_commit = "b".repeat(40);
  const result = evaluateV1RcScorecard(scorecard);
  const ids = new Set(result.failures.map((gate) => gate.id));
  assert.ok(ids.has("beta_evidence.summary_sha256"));
  assert.ok(ids.has("beta_evidence.source_sha256"));
  assert.ok(ids.has("beta_evidence.same_tag"));
  assert.ok(ids.has("beta_evidence.same_commit"));
});

test("GA reuses RC Beta evidence only when it is bound to the GA commit", () => {
  const scorecard = passingScorecard();
  scorecard.release_tag = "v1.0.0";
  scorecard.integration_evidence.release_tag = "v1.0.0";
  for (const item of scorecard.office_compatibility) item.release_ref = "v1.0.0";
  for (const item of scorecard.packages) {
    item.tag = "v1.0.0";
    if (item.kind.startsWith("macos-")) {
      item.developer_id_signed = true;
      item.notarized = true;
    }
  }
  scorecard.integrations.tencent_docs.real_e2e = "passed";
  scorecard.integrations.image_provider.real_e2e = "passed";

  assert.equal(evaluateV1RcScorecard(scorecard, { ga: true }).ok, true);

  scorecard.beta_evidence.release_tag = "v1.0.0";
  let result = evaluateV1RcScorecard(scorecard, { ga: true });
  assert.ok(result.failures.some((gate) => gate.id === "beta_evidence.same_tag"));

  scorecard.beta_evidence.release_tag = "v1.0.0-rc.9";
  scorecard.beta_evidence.release_commit = "b".repeat(40);
  result = evaluateV1RcScorecard(scorecard, { ga: true });
  assert.ok(result.failures.some((gate) => gate.id === "beta_evidence.same_commit"));
});

test("Office artifact evidence proves a changed non-empty versioned file", () => {
  const scorecard = passingScorecard();
  const docx = scorecard.office_compatibility[0].docx;
  docx.final_sha256 = docx.initial_sha256;
  scorecard.office_compatibility[1].xlsx.final_size = 0;
  scorecard.office_compatibility[2].pptx.previous_version_id = "";
  const result = evaluateV1RcScorecard(scorecard);
  const ids = new Set(result.failures.map((gate) => gate.id));
  assert.ok(
    ids.has(`office.${REQUIRED_OFFICE_PLATFORMS[0]}.docx.artifact_checksums`),
  );
  assert.ok(ids.has(`office.${REQUIRED_OFFICE_PLATFORMS[1]}.xlsx.final_size`));
  assert.ok(
    ids.has(`office.${REQUIRED_OFFICE_PLATFORMS[2]}.pptx.previous_version_id`),
  );
});
