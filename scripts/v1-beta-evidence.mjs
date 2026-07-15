#!/usr/bin/env node

import { createHash } from "node:crypto";
import {
  chmodSync,
  constants,
  fstatSync,
  lstatSync,
  linkSync,
  openSync,
  closeSync,
  fsyncSync,
  readFileSync,
  unlinkSync,
  writeFileSync,
} from "node:fs";
import path from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";
import { isDeepStrictEqual } from "node:util";

export const BETA_EVIDENCE_SCHEMA_VERSION = 1;
export const REQUIRED_BETA_WORKFLOWS = Object.freeze([
  "file_organization",
  "office_create_edit",
  "document_read_analysis",
  "connector_read",
  "background_automation",
]);

const RELEASE_TAG_PATTERN = /^v1\.0\.0-rc\.\d+$/u;
const COMMIT_PATTERN = /^(?!0{40}$)[0-9a-f]{40}$/u;
const SHA256_PATTERN = /^(?!0{64}$)[0-9a-f]{64}$/u;
const PARTICIPANT_TOKEN_PATTERN = /^(?!0{64}$)[0-9a-f]{64}$/u;
const MAX_INPUT_BYTES = 64 * 1024 * 1024;
const MAX_LINE_BYTES = 1024 * 1024;
const MAX_RECORDS = 100_000;

export class BetaEvidenceError extends Error {}

function sha256(value) {
  return createHash("sha256").update(value).digest("hex");
}

function requiredString(value, label, { pattern, maxLength = 512 } = {}) {
  if (typeof value !== "string" || value.length === 0 || value !== value.trim()) {
    throw new BetaEvidenceError(`${label} must be a non-empty trimmed string`);
  }
  if (value.length > maxLength || /[\u0000-\u001f\u007f]/u.test(value)) {
    throw new BetaEvidenceError(`${label} contains unsupported characters or is too long`);
  }
  if (pattern && !pattern.test(value)) {
    throw new BetaEvidenceError(`${label} has an invalid format`);
  }
  return value;
}

function exactBoolean(value, label) {
  if (typeof value !== "boolean") {
    throw new BetaEvidenceError(`${label} must be a boolean`);
  }
  return value;
}

function nonnegativeInteger(value, label) {
  if (!Number.isInteger(value) || value < 0) {
    throw new BetaEvidenceError(`${label} must be a non-negative integer`);
  }
  return value;
}

function timestamp(value, label) {
  const text = requiredString(value, label, { maxLength: 64 });
  const parsed = Date.parse(text);
  if (
    !Number.isFinite(parsed) ||
    !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$/u.test(text) ||
    new Date(parsed).toISOString() !== text
  ) {
    throw new BetaEvidenceError(`${label} must be a canonical UTC ISO-8601 timestamp`);
  }
  return { text, parsed };
}

function assertRegularInput(filePath) {
  let info;
  try {
    info = lstatSync(filePath);
  } catch (error) {
    throw new BetaEvidenceError(`Beta evidence input cannot be read: ${error.message}`);
  }
  if (!info.isFile() || info.isSymbolicLink()) {
    throw new BetaEvidenceError("Beta evidence input must be a regular, non-symlink file");
  }
  if (info.size <= 0 || info.size > MAX_INPUT_BYTES) {
    throw new BetaEvidenceError(
      `Beta evidence input must be between 1 and ${MAX_INPUT_BYTES} bytes`,
    );
  }
  return info;
}

function readStableRegularFile(inputPath, label) {
  const filePath = path.resolve(inputPath);
  const before = assertRegularInput(filePath);
  let descriptor = -1;
  let bytes;
  let openedBefore;
  let openedAfter;
  try {
    descriptor = openSync(filePath, constants.O_RDONLY | (constants.O_NOFOLLOW ?? 0));
    openedBefore = fstatSync(descriptor);
    bytes = readFileSync(descriptor);
    openedAfter = fstatSync(descriptor);
  } catch (error) {
    throw new BetaEvidenceError(`${label} could not be opened safely: ${error.message}`);
  } finally {
    if (descriptor >= 0) closeSync(descriptor);
  }
  const after = assertRegularInput(filePath);
  for (const candidate of [openedBefore, openedAfter, after]) {
    if (
      !candidate.isFile() ||
      before.dev !== candidate.dev ||
      before.ino !== candidate.ino ||
      before.size !== candidate.size ||
      before.mtimeMs !== candidate.mtimeMs
    ) {
      throw new BetaEvidenceError(`${label} changed while it was being read`);
    }
  }
  if (bytes.length !== after.size) {
    throw new BetaEvidenceError(`${label} changed while it was being read`);
  }
  return bytes;
}

function parseJsonFile(inputPath, label) {
  const bytes = readStableRegularFile(inputPath, label);
  let value;
  try {
    value = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes));
  } catch (error) {
    throw new BetaEvidenceError(`${label} is not valid UTF-8 JSON: ${error.message}`);
  }
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new BetaEvidenceError(`${label} must contain a JSON object`);
  }
  return { value, bytes };
}

function parseRecord(raw, lineNumber, expected) {
  let value;
  try {
    value = JSON.parse(raw);
  } catch (error) {
    throw new BetaEvidenceError(`line ${lineNumber} is not valid JSON: ${error.message}`);
  }
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new BetaEvidenceError(`line ${lineNumber} must contain a JSON object`);
  }
  const prefix = `line ${lineNumber}`;
  if (value.schema_version !== BETA_EVIDENCE_SCHEMA_VERSION) {
    throw new BetaEvidenceError(`${prefix}.schema_version must equal 1`);
  }
  const taskId = requiredString(value.task_id, `${prefix}.task_id`, { maxLength: 128 });
  const participantToken = requiredString(
    value.participant_token,
    `${prefix}.participant_token`,
    { pattern: PARTICIPANT_TOKEN_PATTERN, maxLength: 64 },
  );
  const releaseTag = requiredString(value.release_tag, `${prefix}.release_tag`, {
    pattern: RELEASE_TAG_PATTERN,
    maxLength: 64,
  });
  const releaseCommit = requiredString(value.release_commit, `${prefix}.release_commit`, {
    pattern: COMMIT_PATTERN,
    maxLength: 40,
  });
  if (releaseTag !== expected.releaseTag || releaseCommit !== expected.releaseCommit) {
    throw new BetaEvidenceError(`${prefix} is not bound to the requested release tag and commit`);
  }
  const workflow = requiredString(value.workflow, `${prefix}.workflow`, { maxLength: 64 });
  if (!REQUIRED_BETA_WORKFLOWS.includes(workflow)) {
    throw new BetaEvidenceError(`${prefix}.workflow is not a v1 core workflow`);
  }
  const started = timestamp(value.started_at, `${prefix}.started_at`);
  const ended = timestamp(value.ended_at, `${prefix}.ended_at`);
  if (ended.parsed < started.parsed) {
    throw new BetaEvidenceError(`${prefix}.ended_at precedes started_at`);
  }
  const succeeded = exactBoolean(value.succeeded, `${prefix}.succeeded`);
  const outputTask = exactBoolean(value.output_task, `${prefix}.output_task`);
  const outputRevalidated = exactBoolean(
    value.output_revalidated,
    `${prefix}.output_revalidated`,
  );
  if (!outputTask && outputRevalidated) {
    throw new BetaEvidenceError(
      `${prefix}.output_revalidated cannot be true when output_task is false`,
    );
  }
  const unrecoverableDataLoss = nonnegativeInteger(
    value.unrecoverable_data_loss,
    `${prefix}.unrecoverable_data_loss`,
  );
  const evidenceSha256 = requiredString(
    value.evidence_sha256,
    `${prefix}.evidence_sha256`,
    { pattern: SHA256_PATTERN, maxLength: 64 },
  );
  const allowed = new Set([
    "schema_version",
    "task_id",
    "participant_token",
    "release_tag",
    "release_commit",
    "workflow",
    "started_at",
    "ended_at",
    "succeeded",
    "output_task",
    "output_revalidated",
    "unrecoverable_data_loss",
    "evidence_sha256",
  ]);
  const unexpected = Object.keys(value).filter((key) => !allowed.has(key));
  if (unexpected.length > 0) {
    throw new BetaEvidenceError(
      `${prefix} contains unsupported fields: ${unexpected.sort().join(", ")}`,
    );
  }
  return {
    taskId,
    participantToken,
    releaseTag,
    releaseCommit,
    workflow,
    startedAt: started,
    endedAt: ended,
    succeeded,
    outputTask,
    outputRevalidated,
    unrecoverableDataLoss,
    evidenceSha256,
  };
}

export function buildBetaEvidenceSummary(
  inputPath,
  { releaseTag, releaseCommit, generatedAt = new Date() },
) {
  const normalizedTag = requiredString(releaseTag, "release tag", {
    pattern: RELEASE_TAG_PATTERN,
    maxLength: 64,
  });
  const normalizedCommit = requiredString(releaseCommit, "release commit", {
    pattern: COMMIT_PATTERN,
    maxLength: 40,
  });
  if (!(generatedAt instanceof Date) || !Number.isFinite(generatedAt.getTime())) {
    throw new BetaEvidenceError("generatedAt must be a valid Date");
  }

  const bytes = readStableRegularFile(inputPath, "Beta evidence input");
  let text;
  try {
    text = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
  } catch {
    throw new BetaEvidenceError("Beta evidence input must contain valid UTF-8");
  }
  const lines = text.split(/\r?\n/u);
  if (lines.at(-1) === "") lines.pop();
  if (lines.length === 0 || lines.length > MAX_RECORDS) {
    throw new BetaEvidenceError(`Beta evidence must contain 1 to ${MAX_RECORDS} records`);
  }

  const records = [];
  const taskIds = new Set();
  const evidenceHashes = new Set();
  for (let index = 0; index < lines.length; index += 1) {
    const lineBytes = Buffer.byteLength(lines[index], "utf8");
    if (lineBytes === 0 || lineBytes > MAX_LINE_BYTES) {
      throw new BetaEvidenceError(
        `line ${index + 1} must be between 1 and ${MAX_LINE_BYTES} bytes`,
      );
    }
    const record = parseRecord(lines[index], index + 1, {
      releaseTag: normalizedTag,
      releaseCommit: normalizedCommit,
    });
    if (taskIds.has(record.taskId)) {
      throw new BetaEvidenceError(`duplicate task_id: ${record.taskId}`);
    }
    if (evidenceHashes.has(record.evidenceSha256)) {
      throw new BetaEvidenceError(
        `duplicate evidence_sha256 cannot prove two independent tasks: ${record.taskId}`,
      );
    }
    taskIds.add(record.taskId);
    evidenceHashes.add(record.evidenceSha256);
    records.push(record);
  }

  const participantTokens = [...new Set(records.map((record) => record.participantToken))].sort();
  const startedAt = Math.min(...records.map((record) => record.startedAt.parsed));
  const endedAt = Math.max(...records.map((record) => record.endedAt.parsed));
  const workflows = {};
  for (const workflow of REQUIRED_BETA_WORKFLOWS) {
    const selected = records.filter((record) => record.workflow === workflow);
    workflows[workflow] = {
      total: selected.length,
      succeeded: selected.filter((record) => record.succeeded).length,
      output_tasks: selected.filter((record) => record.outputTask).length,
      output_revalidated: selected.filter(
        (record) => record.outputTask && record.outputRevalidated,
      ).length,
      unrecoverable_data_loss: selected.reduce(
        (total, record) => total + record.unrecoverableDataLoss,
        0,
      ),
    };
  }

  return {
    schema_version: BETA_EVIDENCE_SCHEMA_VERSION,
    release_tag: normalizedTag,
    release_commit: normalizedCommit,
    generated_at: generatedAt.toISOString(),
    source: {
      kind: "anonymous_task_records_jsonl",
      sha256: sha256(bytes),
      size: bytes.length,
      record_count: records.length,
    },
    privacy: {
      participant_identifiers: "sha256_tokens_only",
      prompts_collected: false,
      file_paths_collected: false,
      participant_tokens: participantTokens,
    },
    beta: {
      controlled_user_count: participantTokens.length,
      started_at: new Date(startedAt).toISOString(),
      ended_at: new Date(endedAt).toISOString(),
      workflows,
    },
  };
}

export function evaluateBetaEvidenceLink(scorecard, summary, summaryBytes) {
  const failures = [];
  const fail = (message) => failures.push(message);
  if (!scorecard || typeof scorecard !== "object" || Array.isArray(scorecard)) {
    return { ok: false, failures: ["scorecard must contain a JSON object"] };
  }
  if (!summary || typeof summary !== "object" || Array.isArray(summary)) {
    return { ok: false, failures: ["Beta summary must contain a JSON object"] };
  }
  if (!Buffer.isBuffer(summaryBytes)) {
    return { ok: false, failures: ["Beta summary bytes are required"] };
  }
  const link = scorecard.beta_evidence;
  if (!link || typeof link !== "object" || Array.isArray(link)) {
    return { ok: false, failures: ["scorecard.beta_evidence is missing"] };
  }
  const summaryHash = sha256(summaryBytes);
  if (link.summary_sha256 !== summaryHash) fail("Beta summary SHA-256 does not match scorecard");
  const scorecardTag = String(scorecard.release_tag ?? "");
  const betaTag = String(link.release_tag ?? "");
  const betaTagAllowed =
    betaTag === scorecardTag ||
    (scorecardTag === "v1.0.0" && RELEASE_TAG_PATTERN.test(betaTag));
  if (!betaTagAllowed) {
    fail("Beta evidence must identify this RC or an RC tag promoted at the same GA commit");
  }
  if (link.release_commit !== scorecard.release_commit) {
    fail("Beta evidence commit does not match scorecard");
  }
  if (summary.schema_version !== BETA_EVIDENCE_SCHEMA_VERSION) {
    fail("Beta evidence schema_version must equal 1");
  }
  if (summary.release_tag !== link.release_tag || !betaTagAllowed) {
    fail("Beta summary release_tag does not match the allowed scorecard link");
  }
  if (
    summary.release_commit !== scorecard.release_commit ||
    summary.release_commit !== link.release_commit
  ) {
    fail("Beta summary release_commit does not match scorecard link");
  }
  if (!summary.source || typeof summary.source !== "object") {
    fail("Beta summary source is missing");
  } else {
    if (!SHA256_PATTERN.test(String(summary.source.sha256 ?? ""))) {
      fail("Beta source SHA-256 is invalid");
    }
    if (link.source_sha256 !== summary.source.sha256) {
      fail("Beta source SHA-256 does not match scorecard link");
    }
    if (!Number.isInteger(summary.source.size) || summary.source.size <= 0) {
      fail("Beta source size must be positive");
    }
  }
  if (!isDeepStrictEqual(scorecard.beta, summary.beta)) {
    fail("scorecard.beta was not copied exactly from the Beta evidence summary");
  }
  const tokens = summary.privacy?.participant_tokens;
  if (
    summary.privacy?.prompts_collected !== false ||
    summary.privacy?.file_paths_collected !== false ||
    summary.privacy?.participant_identifiers !== "sha256_tokens_only"
  ) {
    fail("Beta privacy contract is invalid");
  }
  if (
    !Array.isArray(tokens) ||
    tokens.some((value) => !PARTICIPANT_TOKEN_PATTERN.test(String(value))) ||
    new Set(tokens).size !== tokens?.length
  ) {
    fail("Beta participant tokens are invalid or duplicated");
  } else if (tokens.length !== summary.beta?.controlled_user_count) {
    fail("Beta controlled-user count does not match participant tokens");
  }
  const workflowTotal = REQUIRED_BETA_WORKFLOWS.reduce(
    (total, workflow) => total + Number(summary.beta?.workflows?.[workflow]?.total ?? 0),
    0,
  );
  if (summary.source?.record_count !== workflowTotal) {
    fail("Beta source record count does not match workflow totals");
  }
  try {
    const generated = timestamp(summary.generated_at, "Beta generated_at").parsed;
    const ended = timestamp(summary.beta?.ended_at, "Beta ended_at").parsed;
    if (generated < ended) fail("Beta summary was generated before the observed Beta ended");
  } catch (error) {
    fail(error.message);
  }
  return { ok: failures.length === 0, failures };
}

export function verifyBetaEvidenceLink(scorecardPath, summaryPath) {
  const scorecard = parseJsonFile(scorecardPath, "v1 scorecard").value;
  const parsedSummary = parseJsonFile(summaryPath, "Beta evidence summary");
  return evaluateBetaEvidenceLink(scorecard, parsedSummary.value, parsedSummary.bytes);
}

export function writeBetaEvidenceSummary(outputPath, summary) {
  const destination = path.resolve(outputPath);
  const parent = path.dirname(destination);
  const parentInfo = lstatSync(parent);
  if (!parentInfo.isDirectory() || parentInfo.isSymbolicLink()) {
    throw new BetaEvidenceError("Beta evidence output parent must be a real directory");
  }
  const temporary = `${destination}.tmp-${process.pid}-${Date.now()}`;
  let descriptor = -1;
  try {
    descriptor = openSync(temporary, "wx", 0o600);
    writeFileSync(descriptor, `${JSON.stringify(summary, null, 2)}\n`, "utf8");
    fsyncSync(descriptor);
    closeSync(descriptor);
    descriptor = -1;
    chmodSync(temporary, 0o600);
    // Hard-link publication is a same-volume, no-replace operation. Refusing
    // an existing name avoids a check/rename race that could overwrite an
    // operator's evidence file or a newly inserted symlink.
    try {
      linkSync(temporary, destination);
    } catch (error) {
      if (error.code === "EEXIST") {
        throw new BetaEvidenceError("Beta evidence output already exists");
      }
      throw error;
    }
    unlinkSync(temporary);
  } finally {
    if (descriptor >= 0) closeSync(descriptor);
    try {
      unlinkSync(temporary);
    } catch (error) {
      if (error.code !== "ENOENT") throw error;
    }
  }
}

function parseArguments(argv, allowedNames) {
  const options = {};
  for (let index = 0; index < argv.length; index += 1) {
    const name = argv[index];
    if (!allowedNames.includes(name)) {
      throw new BetaEvidenceError(`unknown argument: ${name}`);
    }
    const value = argv[index + 1];
    if (!value || value.startsWith("--")) {
      throw new BetaEvidenceError(`${name} requires a value`);
    }
    const key = name.slice(2);
    if (Object.hasOwn(options, key)) {
      throw new BetaEvidenceError(`${name} cannot be repeated`);
    }
    options[key] = value;
    index += 1;
  }
  return options;
}

function main() {
  try {
    const [command, ...argv] = process.argv.slice(2);
    if (command === "summarize") {
      const options = parseArguments(argv, [
        "--input",
        "--output",
        "--release-tag",
        "--release-commit",
      ]);
      for (const name of ["input", "output", "release-tag", "release-commit"]) {
        if (!options[name]) throw new BetaEvidenceError(`--${name} is required`);
      }
      const summary = buildBetaEvidenceSummary(options.input, {
        releaseTag: options["release-tag"],
        releaseCommit: options["release-commit"],
      });
      writeBetaEvidenceSummary(options.output, summary);
      process.stdout.write(
        `PASS: ${summary.source.record_count} anonymous Beta task records were summarized.\n`,
      );
    } else if (command === "verify-link") {
      const options = parseArguments(argv, ["--scorecard", "--summary"]);
      for (const name of ["scorecard", "summary"]) {
        if (!options[name]) throw new BetaEvidenceError(`--${name} is required`);
      }
      const result = verifyBetaEvidenceLink(options.scorecard, options.summary);
      if (!result.ok) {
        throw new BetaEvidenceError(result.failures.join("; "));
      }
      process.stdout.write("PASS: scorecard Beta evidence link is valid.\n");
    } else {
      throw new BetaEvidenceError("command must be summarize or verify-link");
    }
  } catch (error) {
    process.stderr.write(`FAIL: ${error.message}\n`);
    process.exitCode = 1;
  }
}

if (
  process.argv[1] &&
  path.resolve(process.argv[1]) === path.resolve(fileURLToPath(import.meta.url))
) {
  main();
}
