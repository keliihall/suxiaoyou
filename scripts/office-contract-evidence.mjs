#!/usr/bin/env node

import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import {
  mkdirSync,
  readFileSync,
  readdirSync,
  renameSync,
  statSync,
  writeFileSync,
} from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

export const OFFICE_CONTRACT_SCHEMA_VERSION = 1;
export const OFFICE_CONTRACT_VERSION = "v1.0-restricted-office-1";
export const REQUIRED_OFFICE_PLATFORMS = Object.freeze([
  "windows-x64",
  "macos-arm64",
  "macos-x64",
  "linux-x64",
  "linux-arm64",
]);
export const REQUIRED_OFFICE_FORMATS = Object.freeze(["docx", "xlsx", "pptx"]);

const SHA256_PATTERN = /^(?!0{64}$)[0-9a-f]{64}$/;
const COMMIT_PATTERN = /^(?!0{40}$)[0-9a-f]{40}$/;
const REQUIRED_FORMAT_GATES = Object.freeze([
  "created",
  "edited",
  "reopened_and_validated",
  "independent_reopen_validated",
  "atomic_install",
  "version_snapshot_verified",
]);

function record(value) {
  return value && typeof value === "object" && !Array.isArray(value) ? value : {};
}

export function resolveCheckoutCommit({
  cwd = process.cwd(),
  environment = process.env,
} = {}) {
  const explicit = String(environment.SUXIAOYOU_RELEASE_COMMIT ?? "")
    .trim()
    .toLowerCase();
  if (explicit) {
    if (!COMMIT_PATTERN.test(explicit)) {
      throw new Error("SUXIAOYOU_RELEASE_COMMIT must be a full Git commit ID");
    }
    return explicit;
  }
  const result = spawnSync("git", ["rev-parse", "HEAD^{commit}"], {
    cwd,
    encoding: "utf8",
    windowsHide: true,
  });
  const commit = String(result.stdout ?? "").trim().toLowerCase();
  if (result.error || result.status !== 0 || !COMMIT_PATTERN.test(commit)) {
    const detail = result.error?.message || String(result.stderr ?? "").trim();
    throw new Error(`Could not resolve checkout commit${detail ? `: ${detail}` : ""}`);
  }
  return commit;
}

function runnerPlatformId(runner) {
  const system = String(runner.system ?? "").trim().toLowerCase();
  const machine = String(runner.machine ?? "").trim().toLowerCase();
  const operatingSystem = { darwin: "macos", linux: "linux", windows: "windows" }[
    system
  ];
  const architecture = {
    amd64: "x64",
    x86_64: "x64",
    arm64: "arm64",
    aarch64: "arm64",
  }[machine];
  return operatingSystem && architecture ? `${operatingSystem}-${architecture}` : null;
}

export function validateOfficeContractReport(
  value,
  {
    expectedPlatform,
    expectedCommit,
    expectedReleaseRef,
    requireFrozen = true,
  } = {},
) {
  const report = record(value);
  const failures = [];
  const platform = String(report.platform ?? "");
  if (report.schema_version !== OFFICE_CONTRACT_SCHEMA_VERSION) {
    failures.push("schema_version must be 1");
  }
  if (report.contract_version !== OFFICE_CONTRACT_VERSION) {
    failures.push(`contract_version must be ${OFFICE_CONTRACT_VERSION}`);
  }
  if (report.status !== "ok" || report.all_passed !== true) {
    failures.push("status/all_passed does not prove a successful contract run");
  }
  if (!REQUIRED_OFFICE_PLATFORMS.includes(platform)) {
    failures.push(`unsupported platform ${platform || "missing"}`);
  }
  if (expectedPlatform && platform !== expectedPlatform) {
    failures.push(`platform is ${platform || "missing"}, expected ${expectedPlatform}`);
  }

  const sourceCommit = String(report.source_commit ?? "").toLowerCase();
  if (!COMMIT_PATTERN.test(sourceCommit)) {
    failures.push("source_commit must be a full 40-character Git commit");
  }
  if (expectedCommit && sourceCommit !== String(expectedCommit).toLowerCase()) {
    failures.push(`source_commit is ${sourceCommit || "missing"}, expected ${expectedCommit}`);
  }
  const releaseRef = String(report.release_ref ?? "");
  if (!releaseRef) failures.push("release_ref is missing");
  if (expectedReleaseRef && releaseRef !== expectedReleaseRef) {
    failures.push(`release_ref is ${releaseRef || "missing"}, expected ${expectedReleaseRef}`);
  }

  const runner = record(report.runner);
  if (!String(runner.system ?? "").trim()) failures.push("runner.system is missing");
  if (!String(runner.machine ?? "").trim()) failures.push("runner.machine is missing");
  if (!/^3\.12\.\d+$/.test(String(runner.python_version ?? ""))) {
    failures.push("runner.python_version must be a Python 3.12 patch release");
  }
  if (requireFrozen && runner.frozen_backend !== true) {
    failures.push("runner.frozen_backend must be true");
  }
  if (runnerPlatformId(runner) !== platform) {
    failures.push(
      `runner system/machine does not match platform ${platform || "missing"}`,
    );
  }
  const startedAt = Date.parse(String(report.started_at ?? ""));
  const completedAt = Date.parse(String(report.completed_at ?? ""));
  if (
    !Number.isFinite(startedAt) ||
    !Number.isFinite(completedAt) ||
    completedAt < startedAt
  ) {
    failures.push("started_at/completed_at must be a valid ordered interval");
  }

  const formats = record(report.formats);
  for (const format of REQUIRED_OFFICE_FORMATS) {
    const result = record(formats[format]);
    for (const gate of REQUIRED_FORMAT_GATES) {
      if (result[gate] !== true) failures.push(`${format}.${gate} must be true`);
    }
    for (const checksum of ["initial_sha256", "final_sha256"]) {
      if (!SHA256_PATTERN.test(String(result[checksum] ?? ""))) {
        failures.push(`${format}.${checksum} must be a SHA-256 digest`);
      }
    }
    if (result.initial_sha256 === result.final_sha256) {
      failures.push(`${format} edit did not change the artifact checksum`);
    }
    if (!Number.isInteger(result.final_size) || result.final_size <= 0) {
      failures.push(`${format}.final_size must be a positive integer`);
    }
    if (!String(result.previous_version_id ?? "").trim()) {
      failures.push(`${format}.previous_version_id is missing`);
    }
  }
  const extras = Object.keys(formats).filter(
    (format) => !REQUIRED_OFFICE_FORMATS.includes(format),
  );
  if (extras.length) failures.push(`unexpected Office formats: ${extras.join(", ")}`);
  return { ok: failures.length === 0, failures, report };
}

function findNamedFiles(root, filename) {
  const results = [];
  for (const entry of readdirSync(root, { withFileTypes: true })) {
    const path = join(root, entry.name);
    if (entry.isDirectory() && !entry.isSymbolicLink()) {
      results.push(...findNamedFiles(path, filename));
    } else if (entry.isFile() && entry.name === filename) {
      results.push(path);
    }
  }
  return results;
}

function sha256(content) {
  return createHash("sha256").update(content).digest("hex");
}

function formatEvidence(report, reportSha256) {
  const formats = Object.fromEntries(
    REQUIRED_OFFICE_FORMATS.map((format) => {
      const result = report.formats[format];
      return [
        format,
        {
          ...Object.fromEntries(REQUIRED_FORMAT_GATES.map((gate) => [gate, result[gate]])),
          initial_sha256: result.initial_sha256,
          final_sha256: result.final_sha256,
          final_size: result.final_size,
          previous_version_id: result.previous_version_id,
        },
      ];
    }),
  );
  return {
    platform: report.platform,
    contract_version: report.contract_version,
    source_commit: report.source_commit,
    release_ref: report.release_ref,
    report_sha256: reportSha256,
    started_at: report.started_at,
    completed_at: report.completed_at,
    runner: report.runner,
    ...formats,
  };
}

export function aggregateOfficeContractEvidence(
  reports,
  { expectedCommit, expectedReleaseRef } = {},
) {
  const normalizedCommit = String(expectedCommit ?? "").trim().toLowerCase();
  if (!COMMIT_PATTERN.test(normalizedCommit)) {
    throw new Error("expectedCommit must be a full non-zero Git commit ID");
  }
  if (!String(expectedReleaseRef ?? "").trim()) {
    throw new Error("expectedReleaseRef is required");
  }
  const entries = [];
  const failures = [];
  for (const item of reports) {
    const raw = typeof item.raw === "string" ? item.raw : JSON.stringify(item.value);
    // Validate the exact bytes whose digest is published.  Callers may supply a
    // parsed convenience value, but it must never let a different raw report
    // receive a trusted SHA-256.
    const value = JSON.parse(raw);
    const validation = validateOfficeContractReport(value, {
      expectedCommit: normalizedCommit,
      expectedReleaseRef,
      requireFrozen: true,
    });
    if (!validation.ok) {
      failures.push(
        `${item.path ?? "report"}: ${validation.failures.join("; ")}`,
      );
      continue;
    }
    entries.push(formatEvidence(validation.report, sha256(raw)));
  }

  for (const platform of REQUIRED_OFFICE_PLATFORMS) {
    const matches = entries.filter((entry) => entry.platform === platform);
    if (matches.length !== 1) {
      failures.push(`${platform}: expected exactly one native report, found ${matches.length}`);
    }
  }
  if (entries.length !== REQUIRED_OFFICE_PLATFORMS.length) {
    failures.push(
      `expected ${REQUIRED_OFFICE_PLATFORMS.length} total native reports, found ${entries.length}`,
    );
  }
  if (failures.length) {
    throw new Error(`Office compatibility evidence is incomplete:\n- ${failures.join("\n- ")}`);
  }

  entries.sort(
    (left, right) =>
      REQUIRED_OFFICE_PLATFORMS.indexOf(left.platform) -
      REQUIRED_OFFICE_PLATFORMS.indexOf(right.platform),
  );
  return {
    schema_version: OFFICE_CONTRACT_SCHEMA_VERSION,
    contract_version: OFFICE_CONTRACT_VERSION,
    source_commit: normalizedCommit,
    release_ref: expectedReleaseRef,
    generated_at: new Date().toISOString(),
    office_compatibility: entries,
  };
}

function writeJsonAtomic(path, value) {
  const destination = resolve(path);
  mkdirSync(dirname(destination), { recursive: true });
  const temporary = `${destination}.tmp-${process.pid}`;
  writeFileSync(temporary, `${JSON.stringify(value, null, 2)}\n`, {
    encoding: "utf8",
    mode: 0o600,
  });
  renameSync(temporary, destination);
}

function runCli() {
  const [command, evidenceRoot, expectedCommit, expectedReleaseRef, outputPath] =
    process.argv.slice(2);
  if (
    command !== "aggregate" ||
    !evidenceRoot ||
    !COMMIT_PATTERN.test(String(expectedCommit ?? "").toLowerCase()) ||
    !expectedReleaseRef ||
    !outputPath
  ) {
    console.error(
      "Usage: node scripts/office-contract-evidence.mjs aggregate " +
        "<artifacts-dir> <40-char-commit> <release-ref> <output.json>",
    );
    process.exitCode = 2;
    return;
  }
  try {
    const root = resolve(evidenceRoot);
    if (!statSync(root).isDirectory()) throw new Error(`${root} is not a directory`);
    const reports = findNamedFiles(root, "office-contract.json").map((path) => {
      const raw = readFileSync(path, "utf8");
      return { path, raw, value: JSON.parse(raw) };
    });
    const evidence = aggregateOfficeContractEvidence(reports, {
      expectedCommit,
      expectedReleaseRef,
    });
    writeJsonAtomic(outputPath, evidence);
    console.log(
      `Verified ${reports.length} native Office reports and wrote ${resolve(outputPath)}`,
    );
  } catch (error) {
    console.error(error instanceof Error ? error.message : String(error));
    process.exitCode = 1;
  }
}

const isCli = process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url);
if (isCli) runCli();
