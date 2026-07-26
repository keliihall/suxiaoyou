import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import {
  OFFICE_CONTRACT_VERSION,
  REQUIRED_OFFICE_FORMATS,
  REQUIRED_OFFICE_PLATFORMS,
  aggregateOfficeContractEvidence,
  resolveCheckoutCommit,
  validateOfficeContractReport,
} from "./office-contract-evidence.mjs";

const COMMIT = "a".repeat(40);
const RELEASE_REF = "v1.0.0-rc.7";

function passingReport(platform) {
  const format = {
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
    contract_version: OFFICE_CONTRACT_VERSION,
    status: "ok",
    all_passed: true,
    platform,
    source_commit: COMMIT,
    release_ref: RELEASE_REF,
    started_at: "2026-07-14T00:00:00Z",
    completed_at: "2026-07-14T00:00:01Z",
    runner: {
      system: platform.startsWith("windows")
        ? "Windows"
        : platform.startsWith("macos")
          ? "Darwin"
          : "Linux",
      machine: platform.endsWith("arm64") ? "arm64" : "x86_64",
      python_version: "3.12.13",
      frozen_backend: true,
    },
    formats: Object.fromEntries(
      REQUIRED_OFFICE_FORMATS.map((name) => [name, { ...format }]),
    ),
  };
}

function git(cwd, ...args) {
  const result = spawnSync("git", args, { cwd, encoding: "utf8" });
  assert.equal(result.status, 0, result.stderr);
  return result.stdout.trim().toLowerCase();
}

test("peels annotated tags to the checkout commit instead of trusting GITHUB_SHA", (t) => {
  const repository = mkdtempSync(join(tmpdir(), "office-annotated-tag-"));
  t.after(() => rmSync(repository, { recursive: true, force: true }));
  git(repository, "init", "--quiet");
  git(repository, "config", "user.name", "Office Contract Test");
  git(repository, "config", "user.email", "office-contract@example.invalid");
  writeFileSync(join(repository, "fixture.txt"), "release candidate\n");
  git(repository, "add", "fixture.txt");
  git(repository, "commit", "--quiet", "-m", "release fixture");
  const commit = git(repository, "rev-parse", "HEAD^{commit}");
  git(repository, "tag", "-a", "v1.0.0-rc.7", "-m", "annotated release");
  const tagObject = git(repository, "rev-parse", "v1.0.0-rc.7");
  assert.notEqual(tagObject, commit);
  git(repository, "checkout", "--quiet", "v1.0.0-rc.7");

  assert.equal(
    resolveCheckoutCommit({
      cwd: repository,
      environment: { GITHUB_SHA: tagObject },
    }),
    commit,
  );
});

test("accepts complete frozen native Office evidence", () => {
  const report = passingReport("macos-arm64");
  const result = validateOfficeContractReport(report, {
    expectedPlatform: "macos-arm64",
    expectedCommit: COMMIT,
    expectedReleaseRef: RELEASE_REF,
  });
  assert.equal(result.ok, true, result.failures.join("\n"));
});

test("fails closed on source-only, partial, or mislabeled evidence", () => {
  const report = passingReport("macos-arm64");
  report.runner.frozen_backend = false;
  report.formats.xlsx.independent_reopen_validated = false;
  report.formats.pptx.version_snapshot_verified = false;
  const result = validateOfficeContractReport(report, {
    expectedPlatform: "macos-x64",
    expectedCommit: "b".repeat(40),
    expectedReleaseRef: "v1.0.0-rc.8",
  });
  assert.equal(result.ok, false);
  assert.ok(result.failures.some((failure) => failure.includes("frozen_backend")));
  assert.ok(result.failures.some((failure) => failure.includes("xlsx.independent")));
  assert.ok(result.failures.some((failure) => failure.includes("pptx.version")));
  assert.ok(result.failures.some((failure) => failure.includes("expected macos-x64")));
  assert.ok(result.failures.some((failure) => failure.includes("source_commit")));
  assert.ok(result.failures.some((failure) => failure.includes("release_ref")));
});

test("aggregates exactly one report for every native release platform", () => {
  const reports = REQUIRED_OFFICE_PLATFORMS.map((platform) => {
    const value = passingReport(platform);
    return { path: `${platform}/office-contract.json`, value, raw: JSON.stringify(value) };
  });
  const result = aggregateOfficeContractEvidence(reports, {
    expectedCommit: COMMIT,
    expectedReleaseRef: RELEASE_REF,
  });
  assert.equal(result.source_commit, COMMIT);
  assert.equal(result.release_ref, RELEASE_REF);
  assert.deepEqual(
    result.office_compatibility.map((item) => item.platform),
    REQUIRED_OFFICE_PLATFORMS,
  );
  for (const item of result.office_compatibility) {
    assert.match(item.report_sha256, /^[0-9a-f]{64}$/);
    assert.equal(item.runner.frozen_backend, true);
    assert.equal(item.docx.atomic_install, true);
  }
});

test("rejects a duplicate platform even when six reports are present", () => {
  const platforms = [...REQUIRED_OFFICE_PLATFORMS];
  platforms[platforms.length - 1] = platforms[0];
  const reports = platforms.map((platform, index) => {
    const value = passingReport(platform);
    return { path: `${index}/office-contract.json`, value, raw: JSON.stringify(value) };
  });
  assert.throws(
    () =>
      aggregateOfficeContractEvidence(reports, {
        expectedCommit: COMMIT,
        expectedReleaseRef: RELEASE_REF,
      }),
    /expected exactly one native report/,
  );
});

test("hashes and validates the same raw Office report bytes", () => {
  const reports = REQUIRED_OFFICE_PLATFORMS.map((platform) => {
    const value = passingReport(platform);
    return { path: `${platform}/office-contract.json`, value, raw: JSON.stringify(value) };
  });
  reports[0].raw = JSON.stringify({ ...reports[0].value, status: "failed" });
  assert.throws(
    () =>
      aggregateOfficeContractEvidence(reports, {
        expectedCommit: COMMIT,
        expectedReleaseRef: RELEASE_REF,
      }),
    /status\/all_passed/u,
  );
});

test("aggregation requires explicit release identity", () => {
  const value = passingReport("macos-arm64");
  assert.throws(
    () => aggregateOfficeContractEvidence([{ value }]),
    /expectedCommit/u,
  );
  assert.throws(
    () =>
      aggregateOfficeContractEvidence([{ value }], {
        expectedCommit: COMMIT,
      }),
    /expectedReleaseRef/u,
  );
});
