import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { mkdtempSync, readFileSync, symlinkSync, writeFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import {
  BetaEvidenceError,
  buildBetaEvidenceSummary,
  evaluateBetaEvidenceLink,
  writeBetaEvidenceSummary,
} from "./v1-beta-evidence.mjs";

const TAG = "v1.0.0-rc.2";
const COMMIT = "a".repeat(40);
const WORKFLOWS = [
  "file_organization",
  "office_create_edit",
  "document_read_analysis",
  "connector_read",
  "background_automation",
];

function record(index, overrides = {}) {
  const participant = ((index % 20) + 1).toString(16).padStart(64, "0");
  return {
    schema_version: 1,
    task_id: `task-${index}`,
    participant_token: participant,
    release_tag: TAG,
    release_commit: COMMIT,
    workflow: WORKFLOWS[index % WORKFLOWS.length],
    started_at: new Date(Date.UTC(2026, 6, 1) + index * 3_600_000).toISOString(),
    ended_at: new Date(Date.UTC(2026, 6, 1) + index * 3_600_000 + 60_000).toISOString(),
    succeeded: index % 20 !== 0,
    output_task: index % 2 === 0,
    output_revalidated: index % 2 === 0,
    unrecoverable_data_loss: 0,
    evidence_sha256: index.toString(16).padStart(64, "0").replace(/^0+$/u, "1".repeat(64)),
    ...overrides,
  };
}

function writeRecords(records) {
  const root = mkdtempSync(path.join(os.tmpdir(), "suyo-beta-evidence-"));
  const input = path.join(root, "tasks.jsonl");
  writeFileSync(input, `${records.map((value) => JSON.stringify(value)).join("\n")}\n`);
  return { root, input };
}

test("summarizes anonymous release-bound Beta records deterministically", () => {
  const records = Array.from({ length: 200 }, (_, index) => record(index));
  // Ensure the observed task window spans at least seven days.
  records[199].ended_at = "2026-07-10T00:00:00.000Z";
  const { root, input } = writeRecords(records);
  const summary = buildBetaEvidenceSummary(input, {
    releaseTag: TAG,
    releaseCommit: COMMIT,
    generatedAt: new Date("2026-07-10T00:00:00.000Z"),
  });

  assert.equal(summary.source.record_count, 200);
  assert.equal(summary.beta.controlled_user_count, 20);
  assert.equal(summary.beta.workflows.file_organization.total, 40);
  assert.equal(summary.beta.workflows.file_organization.succeeded, 30);
  assert.equal(summary.beta.workflows.office_create_edit.output_tasks, 20);
  assert.equal(summary.privacy.prompts_collected, false);
  assert.equal(summary.privacy.file_paths_collected, false);
  assert.equal(summary.privacy.participant_tokens.length, 20);

  const output = path.join(root, "BETA-EVIDENCE.json");
  writeBetaEvidenceSummary(output, summary);
  assert.deepEqual(JSON.parse(readFileSync(output, "utf8")), summary);
});

test("rejects duplicate task and evidence identities", () => {
  let fixture = writeRecords([record(1), record(1, { evidence_sha256: "b".repeat(64) })]);
  assert.throws(
    () => buildBetaEvidenceSummary(fixture.input, { releaseTag: TAG, releaseCommit: COMMIT }),
    /duplicate task_id/u,
  );

  fixture = writeRecords([record(1), record(2, { evidence_sha256: record(1).evidence_sha256 })]);
  assert.throws(
    () => buildBetaEvidenceSummary(fixture.input, { releaseTag: TAG, releaseCommit: COMMIT }),
    /duplicate evidence_sha256/u,
  );
});

test("rejects release mismatch, private extra fields, and invalid result relations", () => {
  for (const overrides of [
    { release_commit: "b".repeat(40) },
    { prompt: "must not be collected" },
    { output_task: false, output_revalidated: true },
    { unrecoverable_data_loss: -1 },
  ]) {
    const { input } = writeRecords([record(1, overrides)]);
    assert.throws(
      () => buildBetaEvidenceSummary(input, { releaseTag: TAG, releaseCommit: COMMIT }),
      BetaEvidenceError,
    );
  }
});

test("rejects symlink input and output", () => {
  const { root, input } = writeRecords([record(1)]);
  const inputLink = path.join(root, "input-link.jsonl");
  symlinkSync(input, inputLink);
  assert.throws(
    () => buildBetaEvidenceSummary(inputLink, { releaseTag: TAG, releaseCommit: COMMIT }),
    /non-symlink/u,
  );

  const summary = buildBetaEvidenceSummary(input, {
    releaseTag: TAG,
    releaseCommit: COMMIT,
  });
  const realOutput = path.join(root, "real-output.json");
  writeFileSync(realOutput, "{}\n");
  const outputLink = path.join(root, "output-link.json");
  symlinkSync(realOutput, outputLink);
  assert.throws(
    () => writeBetaEvidenceSummary(outputLink, summary),
    /already exists/u,
  );
});

test("companion gate binds the scorecard to the exact summary and raw source", () => {
  const records = Array.from({ length: 200 }, (_, index) => record(index));
  records[199].ended_at = "2026-07-10T00:00:00.000Z";
  const { input } = writeRecords(records);
  const summary = buildBetaEvidenceSummary(input, {
    releaseTag: TAG,
    releaseCommit: COMMIT,
    generatedAt: new Date("2026-07-10T01:00:00.000Z"),
  });
  const bytes = Buffer.from(`${JSON.stringify(summary, null, 2)}\n`);
  const summarySha256 = createHash("sha256").update(bytes).digest("hex");
  const scorecard = {
    release_tag: TAG,
    release_commit: COMMIT,
    beta: summary.beta,
    beta_evidence: {
      release_tag: TAG,
      release_commit: COMMIT,
      summary_sha256: summarySha256,
      source_sha256: summary.source.sha256,
    },
  };
  assert.equal(evaluateBetaEvidenceLink(scorecard, summary, bytes).ok, true);

  const tampered = structuredClone(scorecard);
  tampered.beta.workflows.file_organization.succeeded += 1;
  const result = evaluateBetaEvidenceLink(tampered, summary, bytes);
  assert.equal(result.ok, false);
  assert.match(result.failures.join("\n"), /not copied exactly/u);

  const gaScorecard = structuredClone(scorecard);
  gaScorecard.release_tag = "v1.0.0";
  assert.equal(evaluateBetaEvidenceLink(gaScorecard, summary, bytes).ok, true);
  gaScorecard.release_commit = "c".repeat(40);
  assert.equal(evaluateBetaEvidenceLink(gaScorecard, summary, bytes).ok, false);
});
