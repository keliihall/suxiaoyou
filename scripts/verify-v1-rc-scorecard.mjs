#!/usr/bin/env node

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

export const REQUIRED_PACKAGE_KINDS = Object.freeze([
  "windows-x64-nsis",
  "macos-arm64-dmg",
  "macos-x64-dmg",
  "linux-x64-deb",
  "linux-x64-rpm",
  "linux-arm64-deb",
  "linux-arm64-rpm",
]);

export const REQUIRED_OFFICE_PLATFORMS = Object.freeze([
  "windows-x64",
  "macos-arm64",
  "macos-x64",
  "linux-x64",
  "linux-arm64",
]);

export const REQUIRED_BETA_WORKFLOWS = Object.freeze([
  "file_organization",
  "office_create_edit",
  "document_read_analysis",
  "connector_read",
  "background_automation",
]);

const MIN_CONTROLLED_USERS = 20;
const MIN_BETA_TASKS = 200;
const MIN_BETA_DURATION_MS = 7 * 24 * 60 * 60 * 1000;
const MIN_TASK_SUCCESS_RATE = 0.9;
const OFFICE_CONTRACT_VERSION = "v1.0-restricted-office-1";
const GIT_COMMIT_PATTERN = /^(?!0{40}$)[0-9a-f]{40}$/;
const SHA256_PATTERN = /^(?!0{64}$)[0-9a-f]{64}$/;

function object(value) {
  return value && typeof value === "object" && !Array.isArray(value) ? value : {};
}

function array(value) {
  return Array.isArray(value) ? value : [];
}

function number(value) {
  return typeof value === "number" && Number.isFinite(value) ? value : 0;
}

function nonnegativeInteger(value) {
  return Number.isInteger(value) && value >= 0;
}

function addGate(gates, id, passed, detail) {
  gates.push({ id, passed: Boolean(passed), detail });
}

function elapsedMs(startValue, endValue) {
  const start = Date.parse(String(startValue ?? ""));
  const end = Date.parse(String(endValue ?? ""));
  if (!Number.isFinite(start) || !Number.isFinite(end) || end < start) return null;
  return end - start;
}

function evaluatePackages(scorecard, gates, ga) {
  const releaseTag = String(scorecard.release_tag ?? "");
  const releaseCommit = String(scorecard.release_commit ?? "").toLowerCase();
  const packages = array(scorecard.packages);
  const byKind = new Map(packages.map((item) => [String(object(item).kind ?? ""), object(item)]));

  addGate(
    gates,
    "release.tag",
    ga ? releaseTag === "v1.0.0" : /^v1\.0\.0-rc\.\d+$/.test(releaseTag),
    `release_tag=${releaseTag || "missing"}`,
  );
  addGate(
    gates,
    "release.commit",
    GIT_COMMIT_PATTERN.test(releaseCommit),
    `release_commit=${releaseCommit || "missing"}`,
  );
  const unexpectedKinds = packages
    .map((item) => String(object(item).kind ?? ""))
    .filter((kind) => !REQUIRED_PACKAGE_KINDS.includes(kind));
  addGate(
    gates,
    "package.records_exact",
    packages.length === REQUIRED_PACKAGE_KINDS.length && unexpectedKinds.length === 0,
    `count=${packages.length}, unexpected=${unexpectedKinds.join(",") || "none"}`,
  );

  for (const kind of REQUIRED_PACKAGE_KINDS) {
    const item = byKind.get(kind);
    const matches = packages.filter((candidate) => object(candidate).kind === kind);
    addGate(
      gates,
      `package.${kind}.present`,
      matches.length === 1,
      matches.length === 0 ? "missing" : `${matches.length} entries`,
    );
    if (!item || matches.length !== 1) continue;
    addGate(
      gates,
      `package.${kind}.same_tag`,
      item.tag === releaseTag,
      `artifact_tag=${String(item.tag ?? "missing")}`,
    );
    addGate(
      gates,
      `package.${kind}.same_commit`,
      GIT_COMMIT_PATTERN.test(releaseCommit) && item.source_commit === releaseCommit,
      `source_commit=${String(item.source_commit ?? "missing")}`,
    );
    for (const field of [
      "artifact_sha256",
      "lifecycle_report_sha256",
      "executable_sha256",
    ]) {
      addGate(
        gates,
        `package.${kind}.${field}`,
        SHA256_PATTERN.test(String(item[field] ?? "")),
        String(item[field] ?? "missing"),
      );
    }
    if (kind.startsWith("linux-")) {
      const expectedBundleType = kind.endsWith("-deb") ? "deb" : "rpm";
      addGate(
        gates,
        `package.${kind}.tauri_bundle_type`,
        item.tauri_bundle_type === expectedBundleType,
        String(item.tauri_bundle_type ?? "missing"),
      );
      addGate(
        gates,
        `package.${kind}.executable_unpatched_sha256`,
        SHA256_PATTERN.test(String(item.executable_unpatched_sha256 ?? "")),
        String(item.executable_unpatched_sha256 ?? "missing"),
      );
    }
    addGate(
      gates,
      `package.${kind}.executable_size`,
      Number.isSafeInteger(item.executable_size) && item.executable_size > 0,
      String(item.executable_size ?? "missing"),
    );
    addGate(
      gates,
      `package.${kind}.executable_path`,
      typeof item.executable_path === "string" && item.executable_path.trim().length > 0,
      String(item.executable_path ?? "missing"),
    );
    for (const field of [
      "checksum_verified",
      "installed",
      "launched",
      "exited_cleanly",
      "no_orphan_processes",
    ]) {
      addGate(gates, `package.${kind}.${field}`, item[field] === true, String(item[field] ?? false));
    }
    if (kind.startsWith("macos-")) {
      if (ga) {
        addGate(gates, `package.${kind}.developer_id_signed`, item.developer_id_signed === true, String(item.developer_id_signed ?? false));
        addGate(gates, `package.${kind}.notarized`, item.notarized === true, String(item.notarized ?? false));
      } else {
        const trustedRc = item.developer_id_signed === true || (
          item.artifact_profile === "rc-adhoc" && item.trust_boundary_verified === true
        );
        addGate(
          gates,
          `package.${kind}.rc_trust_profile`,
          trustedRc,
          `profile=${String(item.artifact_profile ?? "missing")}`,
        );
      }
    }
  }
  for (const architecture of ["x64", "arm64"]) {
    const deb = byKind.get(`linux-${architecture}-deb`);
    const rpm = byKind.get(`linux-${architecture}-rpm`);
    const sameUnpatchedExecutable =
      deb !== undefined &&
      rpm !== undefined &&
      SHA256_PATTERN.test(String(deb.executable_unpatched_sha256 ?? "")) &&
      deb.executable_unpatched_sha256 === rpm.executable_unpatched_sha256 &&
      Number.isSafeInteger(deb.executable_size) &&
      deb.executable_size > 0 &&
      deb.executable_size === rpm.executable_size;
    addGate(
      gates,
      `package.linux-${architecture}.deb_rpm_executable_identity`,
      sameUnpatchedExecutable,
      sameUnpatchedExecutable
        ? String(deb.executable_unpatched_sha256)
        : "DEB/RPM executable identity differs after restoring the Tauri bundle marker",
    );
  }
}

function evaluateOfficeCompatibility(scorecard, gates) {
  const records = array(scorecard.office_compatibility);
  const releaseCommit = String(scorecard.release_commit ?? "").toLowerCase();
  const releaseTag = String(scorecard.release_tag ?? "");
  const byPlatform = new Map(
    records.map((item) => [String(object(item).platform ?? ""), object(item)]),
  );
  const unexpectedPlatforms = records
    .map((item) => String(object(item).platform ?? ""))
    .filter((platform) => !REQUIRED_OFFICE_PLATFORMS.includes(platform));
  addGate(
    gates,
    "office.records_exact",
    records.length === REQUIRED_OFFICE_PLATFORMS.length &&
      unexpectedPlatforms.length === 0,
    `count=${records.length}, unexpected=${unexpectedPlatforms.join(",") || "none"}`,
  );
  for (const platform of REQUIRED_OFFICE_PLATFORMS) {
    const item = byPlatform.get(platform);
    const matches = records.filter((candidate) => object(candidate).platform === platform);
    addGate(
      gates,
      `office.${platform}.present`,
      matches.length === 1,
      matches.length === 0 ? "missing" : `${matches.length} entries`,
    );
    if (!item || matches.length !== 1) continue;
    addGate(
      gates,
      `office.${platform}.contract_version`,
      item.contract_version === OFFICE_CONTRACT_VERSION,
      String(item.contract_version ?? "missing"),
    );
    addGate(
      gates,
      `office.${platform}.same_commit`,
      GIT_COMMIT_PATTERN.test(releaseCommit) && item.source_commit === releaseCommit,
      `source_commit=${String(item.source_commit ?? "missing")}`,
    );
    addGate(
      gates,
      `office.${platform}.same_release_ref`,
      item.release_ref === releaseTag,
      `release_ref=${String(item.release_ref ?? "missing")}`,
    );
    addGate(
      gates,
      `office.${platform}.report_sha256`,
      SHA256_PATTERN.test(String(item.report_sha256 ?? "")),
      String(item.report_sha256 ?? "missing"),
    );
    addGate(
      gates,
      `office.${platform}.frozen_backend`,
      object(item.runner).frozen_backend === true,
      String(object(item.runner).frozen_backend ?? false),
    );
    for (const format of ["docx", "xlsx", "pptx"]) {
      const result = object(item[format]);
      for (const field of [
        "created",
        "edited",
        "reopened_and_validated",
        "independent_reopen_validated",
        "atomic_install",
        "version_snapshot_verified",
      ]) {
        addGate(
          gates,
          `office.${platform}.${format}.${field}`,
          result[field] === true,
          String(result[field] ?? false),
        );
      }
      const initialSha256 = String(result.initial_sha256 ?? "");
      const finalSha256 = String(result.final_sha256 ?? "");
      addGate(
        gates,
        `office.${platform}.${format}.artifact_checksums`,
        SHA256_PATTERN.test(initialSha256) &&
          SHA256_PATTERN.test(finalSha256) &&
          initialSha256 !== finalSha256,
        `initial=${initialSha256 || "missing"}, final=${finalSha256 || "missing"}`,
      );
      addGate(
        gates,
        `office.${platform}.${format}.final_size`,
        Number.isInteger(result.final_size) && result.final_size > 0,
        String(result.final_size ?? "missing"),
      );
      addGate(
        gates,
        `office.${platform}.${format}.previous_version_id`,
        typeof result.previous_version_id === "string" &&
          result.previous_version_id.trim().length > 0,
        String(result.previous_version_id ?? "missing"),
      );
    }
  }
}

function evaluateBeta(scorecard, gates) {
  const beta = object(scorecard.beta);
  const workflows = object(beta.workflows);
  const duration = elapsedMs(beta.started_at, beta.ended_at);
  addGate(
    gates,
    "beta.controlled_users",
    nonnegativeInteger(beta.controlled_user_count) &&
      beta.controlled_user_count >= MIN_CONTROLLED_USERS,
    `${number(beta.controlled_user_count)}/${MIN_CONTROLLED_USERS}`,
  );
  addGate(
    gates,
    "beta.duration",
    duration !== null && duration >= MIN_BETA_DURATION_MS,
    duration === null ? "invalid date range" : `${(duration / 86_400_000).toFixed(2)} days`,
  );

  let total = 0;
  let succeeded = 0;
  let outputTasks = 0;
  let outputRevalidated = 0;
  let unrecoverableDataLoss = 0;
  for (const workflow of REQUIRED_BETA_WORKFLOWS) {
    const item = object(workflows[workflow]);
    const workflowTotal = number(item.total);
    const workflowSucceeded = number(item.succeeded);
    const workflowOutputTasks = number(item.output_tasks);
    const workflowOutputRevalidated = number(item.output_revalidated);
    const workflowDataLoss = number(item.unrecoverable_data_loss);
    const countsPresent = [
      item.total,
      item.succeeded,
      item.output_tasks,
      item.output_revalidated,
      item.unrecoverable_data_loss,
    ].every(nonnegativeInteger);

    addGate(
      gates,
      `beta.workflow.${workflow}.covered`,
      workflowTotal > 0,
      `${workflowTotal} tasks`,
    );
    addGate(
      gates,
      `beta.workflow.${workflow}.counts_valid`,
      countsPresent &&
        workflowSucceeded >= 0 &&
        workflowSucceeded <= workflowTotal &&
        workflowOutputTasks >= 0 &&
        workflowOutputTasks <= workflowTotal &&
        workflowOutputRevalidated >= 0 &&
        workflowOutputRevalidated <= workflowOutputTasks &&
        workflowDataLoss >= 0,
      `success=${workflowSucceeded}/${workflowTotal}, output=${workflowOutputRevalidated}/${workflowOutputTasks}`,
    );
    total += workflowTotal;
    succeeded += workflowSucceeded;
    outputTasks += workflowOutputTasks;
    outputRevalidated += workflowOutputRevalidated;
    unrecoverableDataLoss += workflowDataLoss;
  }

  const successRate = total > 0 ? succeeded / total : 0;
  const outputRate = outputTasks > 0 ? outputRevalidated / outputTasks : 1;
  addGate(gates, "beta.task_count", total >= MIN_BETA_TASKS, `${total}/${MIN_BETA_TASKS}`);
  addGate(
    gates,
    "beta.task_success_rate",
    total >= MIN_BETA_TASKS && successRate >= MIN_TASK_SUCCESS_RATE,
    `${(successRate * 100).toFixed(2)}%/${(MIN_TASK_SUCCESS_RATE * 100).toFixed(0)}%`,
  );
  addGate(
    gates,
    "beta.output_revalidation_rate",
    outputTasks > 0 && outputRevalidated === outputTasks,
    `${(outputRate * 100).toFixed(2)}% (${outputRevalidated}/${outputTasks})`,
  );
  addGate(
    gates,
    "beta.unrecoverable_data_loss",
    unrecoverableDataLoss === 0,
    String(unrecoverableDataLoss),
  );
}

function evaluateQualityAndIntegrations(scorecard, gates, ga) {
  const quality = object(scorecard.quality);
  addGate(
    gates,
    "quality.open_p0",
    nonnegativeInteger(quality.open_p0) && quality.open_p0 === 0,
    String(quality.open_p0 ?? "missing"),
  );
  addGate(
    gates,
    "quality.open_p1",
    nonnegativeInteger(quality.open_p1) && quality.open_p1 === 0,
    String(quality.open_p1 ?? "missing"),
  );
  for (const field of [
    "backend_full_suite_passed",
    "frontend_full_suite_passed",
    "playwright_core_passed",
    "rust_full_suite_passed",
    "migrations_passed",
    "supply_chain_vulnerabilities_zero",
    "security_boundary_regression_passed",
  ]) {
    addGate(gates, `quality.${field}`, quality[field] === true, String(quality[field] ?? false));
  }

  const integrations = object(scorecard.integrations);
  for (const id of ["tencent_docs", "image_provider"]) {
    const item = object(integrations[id]);
    addGate(gates, `integration.${id}.contract_test`, item.contract_test === "passed", String(item.contract_test ?? "missing"));
    const acceptedRealE2e = ga
      ? item.real_e2e === "passed"
      : ["passed", "pending_credentials"].includes(item.real_e2e);
    addGate(
      gates,
      `integration.${id}.real_e2e`,
      acceptedRealE2e,
      String(item.real_e2e ?? "missing"),
    );
  }
}

function evaluateIntegrationEvidenceLink(scorecard, gates) {
  const link = object(scorecard.integration_evidence);
  const releaseTag = String(scorecard.release_tag ?? "");
  const releaseCommit = String(scorecard.release_commit ?? "").toLowerCase();
  const summaryFile = String(link.summary_file ?? "").trim();
  addGate(
    gates,
    "integration_evidence.summary_file",
    summaryFile.length > 0 && path.basename(summaryFile) === "INTEGRATION-CONTRACTS.json",
    summaryFile || "missing",
  );
  addGate(
    gates,
    "integration_evidence.summary_sha256",
    SHA256_PATTERN.test(String(link.summary_sha256 ?? "")),
    String(link.summary_sha256 ?? "missing"),
  );
  addGate(
    gates,
    "integration_evidence.same_tag",
    link.release_tag === releaseTag,
    `release_tag=${String(link.release_tag ?? "missing")}`,
  );
  addGate(
    gates,
    "integration_evidence.same_commit",
    GIT_COMMIT_PATTERN.test(releaseCommit) && link.release_commit === releaseCommit,
    `release_commit=${String(link.release_commit ?? "missing")}`,
  );
}

function evaluateBetaEvidenceLink(scorecard, gates, ga) {
  const link = object(scorecard.beta_evidence);
  const releaseTag = String(scorecard.release_tag ?? "");
  const releaseCommit = String(scorecard.release_commit ?? "").toLowerCase();
  for (const field of ["summary_sha256", "source_sha256"]) {
    addGate(
      gates,
      `beta_evidence.${field}`,
      SHA256_PATTERN.test(String(link[field] ?? "")),
      String(link[field] ?? "missing"),
    );
  }
  addGate(
    gates,
    "beta_evidence.same_tag",
    ga
      ? /^v1\.0\.0-rc\.\d+$/u.test(String(link.release_tag ?? ""))
      : link.release_tag === releaseTag,
    `release_tag=${String(link.release_tag ?? "missing")}`,
  );
  addGate(
    gates,
    "beta_evidence.same_commit",
    GIT_COMMIT_PATTERN.test(releaseCommit) && link.release_commit === releaseCommit,
    `release_commit=${String(link.release_commit ?? "missing")}`,
  );
}

export function evaluateV1RcScorecard(value, { ga = false } = {}) {
  const scorecard = object(value);
  const gates = [];
  addGate(gates, "schema_version", scorecard.schema_version === 1, String(scorecard.schema_version ?? "missing"));
  evaluatePackages(scorecard, gates, ga);
  evaluateOfficeCompatibility(scorecard, gates);
  evaluateBeta(scorecard, gates);
  evaluateQualityAndIntegrations(scorecard, gates, ga);
  evaluateIntegrationEvidenceLink(scorecard, gates);
  evaluateBetaEvidenceLink(scorecard, gates, ga);
  const failures = gates.filter((gate) => !gate.passed);
  return {
    ok: failures.length === 0,
    mode: ga ? "ga" : "rc",
    passed: gates.length - failures.length,
    total: gates.length,
    gates,
    failures,
  };
}

function formatResult(result) {
  const lines = [
    `suyo v1.0 ${result.mode.toUpperCase()} scorecard: ${result.ok ? "PASS" : "FAIL"}`,
    `Gates: ${result.passed}/${result.total}`,
  ];
  if (result.failures.length) {
    lines.push("Failed gates:");
    for (const gate of result.failures) lines.push(`- ${gate.id}: ${gate.detail}`);
  }
  return lines.join("\n");
}

const isCli = process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url);
if (isCli) {
  const args = process.argv.slice(2);
  const ga = args.includes("--ga");
  const json = args.includes("--json");
  const file = args.find((arg) => !arg.startsWith("--"));
  if (!file) {
    console.error("Usage: node scripts/verify-v1-rc-scorecard.mjs <scorecard.json> [--ga] [--json]");
    process.exitCode = 2;
  } else {
    try {
      const payload = JSON.parse(fs.readFileSync(file, "utf8"));
      const result = evaluateV1RcScorecard(payload, { ga });
      console.log(json ? JSON.stringify(result, null, 2) : formatResult(result));
      if (!result.ok) process.exitCode = 1;
    } catch (error) {
      console.error(`Could not validate ${file}: ${error instanceof Error ? error.message : String(error)}`);
      process.exitCode = 2;
    }
  }
}
