#!/usr/bin/env node

import { spawnSync } from "node:child_process";
import { createHash, randomUUID } from "node:crypto";
import {
  chmodSync,
  existsSync,
  lstatSync,
  mkdirSync,
  openSync,
  closeSync,
  readFileSync,
  readdirSync,
  renameSync,
  unlinkSync,
  writeFileSync,
} from "node:fs";
import path from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";

export const EVIDENCE_SCHEMA_VERSION = 1;
export const DEFAULT_MAX_EVIDENCE_AGE_HOURS = 7 * 24;
export const TENCENT_WRITE_ACK =
  "I_UNDERSTAND_THIS_MODIFIES_A_DEDICATED_TEST_DOCUMENT";
export const IMAGE_PAID_REQUEST_ACK =
  "I_UNDERSTAND_THIS_MAY_USE_PROVIDER_QUOTA_OR_INCUR_COST";
export const IMAGE_MAX_REQUESTS = 1;

const SCRIPT_PATH = fileURLToPath(import.meta.url);
const REPO_ROOT = path.resolve(path.dirname(SCRIPT_PATH), "..");
const BACKEND_ROOT = path.join(REPO_ROOT, "backend");
const DEFAULT_EVIDENCE_DIR = path.join(
  REPO_ROOT,
  ".codex-artifacts",
  "v1-real-integrations",
);
const TENCENT_MARKER_PLACEHOLDER = "{{SUYO_TENCENT_DOCS_E2E_MARKER}}";
const TENCENT_BASELINE_PLACEHOLDER = "{{SUYO_TENCENT_DOCS_E2E_BASELINE}}";
const MAX_PRICING_AGE_DAYS = 30;

const TARGETS = Object.freeze({
  "tencent-contract": Object.freeze({
    gateId: "integration.tencent_docs.contract",
    kind: "contract",
    args: [
      "-m",
      "pytest",
      "-q",
      "tests/test_mcp/test_tencent_docs_contract.py",
      "-k",
      "not optional_real_server",
    ],
    assertions: [
      "official endpoint and raw Authorization contract",
      "tool allowlist and approval classification",
      "credential-store secrecy and error redaction",
      "reversible write cleanup after ambiguous failure",
    ],
    realRequestsMax: 0,
  }),
  "siliconflow-image-contract": Object.freeze({
    gateId: "integration.image_provider.contract",
    kind: "contract",
    args: [
      "-m",
      "pytest",
      "-q",
      "tests/test_image_generation/test_siliconflow.py",
      "tests/test_image_generation/test_ledger.py",
      "tests/test_tool/test_image_generate.py",
      "-k",
      "not optional_real_siliconflow_image_contract",
    ],
    assertions: [
      "bounded provider request and PNG validation",
      "credential and signed-URL redaction",
      "durable billing-uncertain ledger",
      "atomic local output and replay without a second provider request",
    ],
    realRequestsMax: 0,
  }),
  "tencent-real-write": Object.freeze({
    gateId: "integration.tencent_docs.real_e2e",
    kind: "real_e2e",
    args: [
      "-m",
      "pytest",
      "-q",
      "tests/test_mcp/test_tencent_docs_contract.py::test_optional_real_server_reversible_write_cycle",
    ],
    assertions: [
      "real-account authentication and tools/list",
      "baseline read from a dedicated fixture",
      "random-marker write and read-back",
      "restore in finally and baseline read-back",
    ],
    realRequestsMax: 8,
    restoreAttemptedOnAmbiguousFailure: true,
  }),
  "siliconflow-image-real": Object.freeze({
    gateId: "integration.image_provider.real_e2e",
    kind: "real_e2e",
    args: [
      "-m",
      "pytest",
      "-q",
      "tests/test_integration/test_siliconflow_image_real_e2e.py::test_optional_real_siliconflow_tool_closure",
    ],
    assertions: [
      "real provider credential and available quota",
      "one provider request produces a validated PNG",
      "atomic workspace save and completed durable ledger row",
      "same call id replays locally without a second provider request",
    ],
    realRequestsMax: IMAGE_MAX_REQUESTS,
  }),
});

const REQUIRED_GATES = Object.freeze({
  contract: Object.freeze([
    "integration.tencent_docs.contract",
    "integration.image_provider.contract",
  ]),
  rc: Object.freeze([
    "integration.tencent_docs.contract",
    "integration.image_provider.contract",
  ]),
  ga: Object.freeze([
    "integration.tencent_docs.contract",
    "integration.image_provider.contract",
    "integration.tencent_docs.real_e2e",
    "integration.image_provider.real_e2e",
  ]),
});

export class IntegrationGateError extends Error {}

function sha256(value) {
  return createHash("sha256").update(value).digest("hex");
}

function readJson(filePath, label) {
  let parsed;
  try {
    parsed = JSON.parse(readFileSync(filePath, "utf8"));
  } catch (error) {
    throw new IntegrationGateError(`${label} is not valid JSON: ${error.message}`);
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new IntegrationGateError(`${label} must contain a JSON object`);
  }
  return parsed;
}

function requiredTrimmed(environment, name, { secret = false } = {}) {
  const raw = environment[name];
  if (typeof raw !== "string" || raw.length === 0) {
    throw new IntegrationGateError(`${name} is required`);
  }
  if (raw !== raw.trim() || /[\u0000-\u001f\u007f]/u.test(raw)) {
    throw new IntegrationGateError(`${name} contains surrounding whitespace or control characters`);
  }
  if (raw.length > 16_384) {
    throw new IntegrationGateError(`${name} exceeds the safety limit`);
  }
  if (secret && /^(?:secret|token|api[-_ ]?key|your[-_ ].*|<.*>)$/iu.test(raw)) {
    throw new IntegrationGateError(`${name} still contains a placeholder value`);
  }
  return raw;
}

function jsonObjectFromEnvironment(environment, name) {
  const raw = requiredTrimmed(environment, name);
  let value;
  try {
    value = JSON.parse(raw);
  } catch (error) {
    throw new IntegrationGateError(`${name} must contain valid JSON: ${error.message}`);
  }
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new IntegrationGateError(`${name} must contain a JSON object`);
  }
  return value;
}

function nestedStringContains(value, expected) {
  if (typeof value === "string") return value.includes(expected);
  if (Array.isArray(value)) return value.some((item) => nestedStringContains(item, expected));
  if (value && typeof value === "object") {
    return Object.values(value).some((item) => nestedStringContains(item, expected));
  }
  return false;
}

function globMatches(pattern, value) {
  const escaped = pattern.replace(/[.+?^${}()|[\]\\]/gu, "\\$&").replaceAll("*", ".*");
  return new RegExp(`^${escaped}$`, "u").test(value);
}

function loadTencentCatalog(repoRoot = REPO_ROOT) {
  const catalogPath = path.join(repoRoot, "backend", "app", "data", "connectors.json");
  const entry = readJson(catalogPath, "connector catalog")["tencent-docs"];
  if (!entry || typeof entry !== "object" || Array.isArray(entry)) {
    throw new IntegrationGateError("connector catalog is missing tencent-docs");
  }
  return entry;
}

function loadImagePricing(repoRoot = REPO_ROOT) {
  const sourcePath = path.join(
    repoRoot,
    "backend",
    "app",
    "image_generation",
    "siliconflow.py",
  );
  const source = readFileSync(sourcePath, "utf8");
  const cost = source.match(/SILICONFLOW_IMAGE_ESTIMATED_COST_CNY\s*=\s*([0-9]+(?:\.[0-9]+)?)/u);
  const asOf = source.match(/SILICONFLOW_IMAGE_PRICING_AS_OF\s*=\s*["']([^"']+)["']/u);
  const model = source.match(/SILICONFLOW_IMAGE_MODEL\s*=\s*["']([^"']+)["']/u);
  const sourceUrl = source.match(
    /SILICONFLOW_IMAGE_PRICING_SOURCE_URL\s*=\s*["']([^"']+)["']/u,
  );
  if (!cost || !asOf || !model || !sourceUrl) {
    throw new IntegrationGateError("SiliconFlow pricing contract could not be read");
  }
  return {
    estimatedCostCny: Number(cost[1]),
    pricingAsOf: asOf[1],
    pricingSourceUrl: sourceUrl[1],
    model: model[1],
  };
}

function validateTencentWritePreflight(environment, repoRoot) {
  const acknowledgement = requiredTrimmed(environment, "TENCENT_DOCS_E2E_ALLOW_WRITE");
  if (acknowledgement !== TENCENT_WRITE_ACK) {
    throw new IntegrationGateError(
      `TENCENT_DOCS_E2E_ALLOW_WRITE must exactly equal ${TENCENT_WRITE_ACK}`,
    );
  }
  requiredTrimmed(environment, "TENCENT_DOCS_E2E_TOKEN", { secret: true });
  const documentId = requiredTrimmed(environment, "TENCENT_DOCS_E2E_TEST_DOCUMENT_ID", {
    secret: true,
  });
  const baseline = requiredTrimmed(environment, "TENCENT_DOCS_E2E_BASELINE_TEXT", {
    secret: true,
  });
  if (baseline.length > 4_000) {
    throw new IntegrationGateError("TENCENT_DOCS_E2E_BASELINE_TEXT exceeds 4000 characters");
  }

  const readArgs = jsonObjectFromEnvironment(environment, "TENCENT_DOCS_E2E_READ_ARGS_JSON");
  const writeArgs = jsonObjectFromEnvironment(environment, "TENCENT_DOCS_E2E_WRITE_ARGS_JSON");
  const restoreArgs = jsonObjectFromEnvironment(environment, "TENCENT_DOCS_E2E_RESTORE_ARGS_JSON");
  for (const [name, value] of [
    ["TENCENT_DOCS_E2E_READ_ARGS_JSON", readArgs],
    ["TENCENT_DOCS_E2E_WRITE_ARGS_JSON", writeArgs],
    ["TENCENT_DOCS_E2E_RESTORE_ARGS_JSON", restoreArgs],
  ]) {
    if (!nestedStringContains(value, documentId)) {
      throw new IntegrationGateError(`${name} must reference TENCENT_DOCS_E2E_TEST_DOCUMENT_ID`);
    }
  }
  if (!nestedStringContains(writeArgs, TENCENT_MARKER_PLACEHOLDER)) {
    throw new IntegrationGateError(
      `TENCENT_DOCS_E2E_WRITE_ARGS_JSON must contain ${TENCENT_MARKER_PLACEHOLDER}`,
    );
  }
  if (!nestedStringContains(restoreArgs, TENCENT_BASELINE_PLACEHOLDER)) {
    throw new IntegrationGateError(
      `TENCENT_DOCS_E2E_RESTORE_ARGS_JSON must contain ${TENCENT_BASELINE_PLACEHOLDER}`,
    );
  }

  const readTool = environment.TENCENT_DOCS_E2E_READ_TOOL
    ? requiredTrimmed(environment, "TENCENT_DOCS_E2E_READ_TOOL")
    : "get_content";
  const writeTool = requiredTrimmed(environment, "TENCENT_DOCS_E2E_WRITE_TOOL");
  const restoreTool = requiredTrimmed(environment, "TENCENT_DOCS_E2E_RESTORE_TOOL");
  const catalog = loadTencentCatalog(repoRoot);
  const allowed = Array.isArray(catalog.allowed_tool_patterns)
    ? catalog.allowed_tool_patterns.map(String)
    : [];
  const approval = Array.isArray(catalog.approval_required_tool_patterns)
    ? catalog.approval_required_tool_patterns.map(String)
    : [];
  const isAllowed = (name) => allowed.some((pattern) => globMatches(pattern, name));
  const needsApproval = (name) => approval.some((pattern) => globMatches(pattern, name));
  if (!isAllowed(readTool) || needsApproval(readTool)) {
    throw new IntegrationGateError("configured Tencent read tool must be allowlisted and read-only");
  }
  for (const [label, name] of [
    ["write", writeTool],
    ["restore", restoreTool],
  ]) {
    if (!isAllowed(name) || !needsApproval(name)) {
      throw new IntegrationGateError(
        `configured Tencent ${label} tool must be allowlisted and approval-required`,
      );
    }
  }
  return {
    target: "tencent-real-write",
    ready: true,
    credentialPresent: true,
    dedicatedFixtureBound: true,
    reversibleRestoreConfigured: true,
    toolPolicyValidated: true,
  };
}

function validateImagePreflight(environment, repoRoot, now) {
  requiredTrimmed(environment, "SILICONFLOW_IMAGE_E2E_API_KEY", { secret: true });
  const acknowledgement = requiredTrimmed(
    environment,
    "SILICONFLOW_IMAGE_E2E_ALLOW_PAID_REQUEST",
  );
  if (acknowledgement !== IMAGE_PAID_REQUEST_ACK) {
    throw new IntegrationGateError(
      `SILICONFLOW_IMAGE_E2E_ALLOW_PAID_REQUEST must exactly equal ${IMAGE_PAID_REQUEST_ACK}`,
    );
  }
  const maximumRequests = requiredTrimmed(
    environment,
    "SILICONFLOW_IMAGE_E2E_MAX_REQUESTS",
  );
  if (maximumRequests !== String(IMAGE_MAX_REQUESTS)) {
    throw new IntegrationGateError(
      `SILICONFLOW_IMAGE_E2E_MAX_REQUESTS must exactly equal ${IMAGE_MAX_REQUESTS}`,
    );
  }
  const budgetText = requiredTrimmed(environment, "SILICONFLOW_IMAGE_E2E_MAX_COST_CNY");
  if (!/^(?:0|[1-9][0-9]*)(?:\.[0-9]{1,4})?$/u.test(budgetText)) {
    throw new IntegrationGateError(
      "SILICONFLOW_IMAGE_E2E_MAX_COST_CNY must be a non-negative decimal with at most 4 places",
    );
  }
  const maximumCostCny = Number(budgetText);
  if (!Number.isFinite(maximumCostCny) || maximumCostCny > 10_000) {
    throw new IntegrationGateError("SILICONFLOW_IMAGE_E2E_MAX_COST_CNY is outside the safety limit");
  }
  const pricing = loadImagePricing(repoRoot);
  if (pricing.estimatedCostCny > maximumCostCny) {
    throw new IntegrationGateError(
      `catalog estimate CNY ${pricing.estimatedCostCny} exceeds the acknowledged maximum CNY ${maximumCostCny}`,
    );
  }
  const pricingAt = Date.parse(`${pricing.pricingAsOf}T00:00:00.000Z`);
  const pricingAgeDays = (now.getTime() - pricingAt) / 86_400_000;
  if (
    !Number.isFinite(pricingAt)
    || pricingAgeDays < -1
    || pricingAgeDays > MAX_PRICING_AGE_DAYS
  ) {
    throw new IntegrationGateError(
      `SiliconFlow catalog pricing must be reviewed within ${MAX_PRICING_AGE_DAYS} days`,
    );
  }
  let pricingUrl;
  try {
    pricingUrl = new URL(pricing.pricingSourceUrl);
  } catch {
    throw new IntegrationGateError("SiliconFlow pricing source URL is invalid");
  }
  if (pricingUrl.protocol !== "https:" || pricingUrl.hostname !== "siliconflow.cn") {
    throw new IntegrationGateError("SiliconFlow pricing source must use the official HTTPS domain");
  }
  return {
    target: "siliconflow-image-real",
    ready: true,
    credentialPresent: true,
    maximumProviderRequests: IMAGE_MAX_REQUESTS,
    maximumAcceptedCostCny: maximumCostCny,
    catalogEstimatedCostCny: pricing.estimatedCostCny,
    pricingAsOf: pricing.pricingAsOf,
    pricingSourceUrl: pricing.pricingSourceUrl,
    pricingAgeDays: Number(pricingAgeDays.toFixed(2)),
    model: pricing.model,
    automaticRetry: false,
  };
}

export function preflightTarget(
  target,
  environment = process.env,
  repoRoot = REPO_ROOT,
  now = new Date(),
) {
  if (target === "tencent-real-write") {
    return validateTencentWritePreflight(environment, repoRoot);
  }
  if (target === "siliconflow-image-real") {
    return validateImagePreflight(environment, repoRoot, now);
  }
  throw new IntegrationGateError(`preflight is only available for live targets, got ${target}`);
}

function pythonExecutable(repoRoot = REPO_ROOT, environment = process.env) {
  if (environment.SUYO_INTEGRATION_PYTHON) {
    return environment.SUYO_INTEGRATION_PYTHON;
  }
  const names = process.platform === "win32"
    ? [
        path.join(repoRoot, "backend", "venv", "Scripts", "python.exe"),
        path.join(repoRoot, "backend", ".venv", "Scripts", "python.exe"),
      ]
    : [
        path.join(repoRoot, "backend", "venv", "bin", "python"),
        path.join(repoRoot, "backend", ".venv", "bin", "python"),
      ];
  const candidates = [
    ...names.filter(existsSync),
    process.platform === "win32" ? "python" : "python3",
  ];
  for (const candidate of candidates) {
    const probe = spawnSync(candidate, ["-c", "import pytest"], {
      cwd: path.join(repoRoot, "backend"),
      encoding: "utf8",
      timeout: 10_000,
      windowsHide: true,
    });
    if (probe.status === 0) return candidate;
  }
  throw new IntegrationGateError(
    "no Python interpreter with pytest is available; set SUYO_INTEGRATION_PYTHON",
  );
}

function git(args, repoRoot = REPO_ROOT) {
  const result = spawnSync("git", args, {
    cwd: repoRoot,
    encoding: "utf8",
    windowsHide: true,
  });
  if (result.status !== 0) {
    throw new IntegrationGateError(`git ${args.join(" ")} failed`);
  }
  return result.stdout.trim();
}

function sourceState(repoRoot = REPO_ROOT) {
  return {
    commit: git(["rev-parse", "HEAD"], repoRoot),
    dirty: git(["status", "--porcelain", "--untracked-files=normal"], repoRoot).length > 0,
    releaseTags: git(["tag", "--points-at", "HEAD"], repoRoot)
      .split(/\r?\n/u)
      .map((item) => item.trim())
      .filter(Boolean),
  };
}

function secretValuesForTarget(target, environment) {
  const names = target.startsWith("tencent-")
    ? [
        "TENCENT_DOCS_E2E_TOKEN",
        "TENCENT_DOCS_E2E_TEST_DOCUMENT_ID",
        "TENCENT_DOCS_E2E_BASELINE_TEXT",
        "TENCENT_DOCS_E2E_READ_ARGS_JSON",
        "TENCENT_DOCS_E2E_WRITE_ARGS_JSON",
        "TENCENT_DOCS_E2E_RESTORE_ARGS_JSON",
      ]
    : ["SILICONFLOW_IMAGE_E2E_API_KEY"];
  return names
    .map((name) => [name, environment[name]])
    .filter(([, value]) => typeof value === "string" && value.length > 0)
    .sort((left, right) => right[1].length - left[1].length);
}

function redact(text, values) {
  let redacted = String(text ?? "");
  for (const [name, value] of values) {
    redacted = redacted.split(value).join(`[redacted:${name}]`);
  }
  return redacted;
}

function pytestSummary(output) {
  const matches = [...output.matchAll(/(?:^|\s)([0-9]+) (passed|skipped|failed|error(?:s)?)(?=,|\s|$)/gmu)];
  const counts = { passed: 0, skipped: 0, failed: 0, errors: 0 };
  for (const match of matches) {
    const amount = Number(match[1]);
    if (match[2] === "passed") counts.passed = Math.max(counts.passed, amount);
    else if (match[2] === "skipped") counts.skipped = Math.max(counts.skipped, amount);
    else if (match[2] === "failed") counts.failed = Math.max(counts.failed, amount);
    else counts.errors = Math.max(counts.errors, amount);
  }
  return counts;
}

function failureClassification(output) {
  if (/HTTP (?:401|403)\b/u.test(output)) return "credential_rejected";
  if (/HTTP 402\b/u.test(output)) return "quota_or_billing_required";
  if (/HTTP 429\b/u.test(output)) return "quota_or_rate_limited";
  if (/may have been accepted|check provider billing|billing_status=uncertain/iu.test(output)) {
    return "billing_uncertain_manual_review";
  }
  if (/restore|baseline was not restored|marker remains/iu.test(output)) {
    return "restore_failed_manual_cleanup";
  }
  if (/timed? ?out|timeout/iu.test(output)) return "transport_timeout";
  return "test_failure";
}

function manualActionForFailure(classification) {
  if (classification === "billing_uncertain_manual_review") {
    return "Check the SiliconFlow billing/usage console; do not auto-retry this operation.";
  }
  if (classification === "restore_failed_manual_cleanup") {
    return "Inspect and restore the dedicated Tencent Docs fixture manually before another run.";
  }
  if (classification === "quota_or_billing_required" || classification === "quota_or_rate_limited") {
    return "Check provider quota, rate limits, and billing before an explicitly approved new run.";
  }
  if (classification === "credential_rejected") {
    return "Rotate or reissue the test credential through the provider console; do not paste it into logs.";
  }
  return null;
}

function evidenceEligibility(testPassed, source) {
  const failures = [];
  if (!testPassed) failures.push("test did not pass without skips");
  if (source.dirty) failures.push("worktree is dirty");
  if (!source.stableDuringRun) failures.push("source state changed during the run");
  const eligibleTags = source.releaseTags.filter((tag) =>
    /^v1\.0\.0(?:-rc\.[1-9][0-9]*)?$/u.test(tag),
  );
  if (eligibleTags.length !== 1) {
    failures.push(`expected exactly one v1.0 release tag at HEAD, found ${eligibleTags.length}`);
  }
  return { eligible: failures.length === 0, failures, eligibleTags };
}

function ensureEvidenceDirectory(directory) {
  mkdirSync(directory, { recursive: true, mode: 0o700 });
  const stat = lstatSync(directory);
  if (!stat.isDirectory() || stat.isSymbolicLink()) {
    throw new IntegrationGateError(`evidence directory must be a real directory: ${directory}`);
  }
  try {
    chmodSync(directory, 0o700);
  } catch {
    // Windows does not implement POSIX directory modes; integrity checks below
    // still reject symlinked evidence files.
  }
}

function atomicWriteNew(filePath, value) {
  const directory = path.dirname(filePath);
  const temporary = path.join(directory, `.${path.basename(filePath)}.${randomUUID()}.tmp`);
  let descriptor;
  try {
    descriptor = openSync(temporary, "wx", 0o600);
    writeFileSync(descriptor, value, "utf8");
    closeSync(descriptor);
    descriptor = undefined;
    if (existsSync(filePath)) {
      throw new IntegrationGateError(`refusing to overwrite evidence: ${filePath}`);
    }
    renameSync(temporary, filePath);
  } finally {
    if (descriptor !== undefined) closeSync(descriptor);
    if (existsSync(temporary)) unlinkSync(temporary);
  }
}

function evidenceStem(target, testedAt, commit) {
  return `${target}-${testedAt.replace(/[:.]/gu, "-")}-${commit.slice(0, 12)}.integration-evidence`;
}

export function runTarget(
  target,
  {
    evidenceDirectory = DEFAULT_EVIDENCE_DIR,
    environment = process.env,
    repoRoot = REPO_ROOT,
  } = {},
) {
  const definition = TARGETS[target];
  if (!definition) throw new IntegrationGateError(`unknown integration target: ${target}`);
  const preflight = definition.kind === "real_e2e"
    ? preflightTarget(target, environment, repoRoot)
    : { target, ready: true, credentialFree: true };
  const sourceBefore = sourceState(repoRoot);
  const python = pythonExecutable(repoRoot, environment);
  const startedAt = new Date();
  const result = spawnSync(python, definition.args, {
    cwd: path.join(repoRoot, "backend"),
    env: { ...environment, PYTHONUNBUFFERED: "1" },
    encoding: "utf8",
    maxBuffer: 12 * 1024 * 1024,
    timeout: definition.kind === "real_e2e" ? 5 * 60_000 : 3 * 60_000,
    windowsHide: true,
  });
  const testedAt = new Date().toISOString();
  const sourceAfter = sourceState(repoRoot);
  const sourceStable =
    sourceBefore.commit === sourceAfter.commit &&
    sourceBefore.dirty === sourceAfter.dirty &&
    JSON.stringify(sourceBefore.releaseTags) === JSON.stringify(sourceAfter.releaseTags);
  const sensitiveValues = secretValuesForTarget(target, environment);
  const combined = redact(
    [result.stdout, result.stderr, result.error?.message].filter(Boolean).join("\n"),
    sensitiveValues,
  );
  const summary = pytestSummary(combined);
  const testPassed =
    result.status === 0 && summary.passed > 0 && summary.skipped === 0 && sourceStable;
  const eligibility = evidenceEligibility(testPassed, {
    ...sourceAfter,
    stableDuringRun: sourceStable,
  });
  const classification = testPassed ? null : failureClassification(combined);
  const durationMs = Math.max(0, Date.now() - startedAt.getTime());

  ensureEvidenceDirectory(evidenceDirectory);
  const stem = evidenceStem(target, testedAt, sourceAfter.commit);
  const logName = `${stem}.log`;
  const manifestName = `${stem}.json`;
  const logPath = path.join(evidenceDirectory, logName);
  const manifestPath = path.join(evidenceDirectory, manifestName);
  const log = `${combined.trim()}\n`;
  atomicWriteNew(logPath, log);

  const manifest = {
    schema_version: EVIDENCE_SCHEMA_VERSION,
    gate_id: definition.gateId,
    target,
    evidence_kind: definition.kind,
    status: testPassed ? "passed" : "failed",
    evidence_eligible: eligibility.eligible,
    evidence_eligibility_failures: eligibility.failures,
    tested_at: testedAt,
    duration_ms: durationMs,
    source: {
      commit: sourceAfter.commit,
      dirty: sourceAfter.dirty,
      release_tags: sourceAfter.releaseTags,
      stable_during_run: sourceStable,
    },
    runner: {
      script_sha256: sha256(readFileSync(path.join(repoRoot, "scripts", path.basename(SCRIPT_PATH)))),
      python,
      pytest_args: definition.args,
      automatic_retry: false,
    },
    preflight,
    contract: {
      assertions: definition.assertions,
      real_provider_requests_max: definition.realRequestsMax,
      restore_attempted_on_ambiguous_failure:
        definition.restoreAttemptedOnAmbiguousFailure ?? false,
    },
    result: {
      exit_code: result.status,
      signal: result.signal ?? null,
      timed_out: result.error?.code === "ETIMEDOUT",
      pytest: summary,
      failure_class: classification,
      manual_action_required: manualActionForFailure(classification),
    },
    log: {
      file: logName,
      sha256: sha256(log),
      bytes: Buffer.byteLength(log),
      secrets_redacted: sensitiveValues.map(([name]) => name),
    },
  };
  atomicWriteNew(manifestPath, `${JSON.stringify(manifest, null, 2)}\n`);
  return {
    testPassed,
    evidenceEligible: eligibility.eligible,
    manifest,
    manifestPath,
    logPath,
  };
}

function evidenceFiles(directory) {
  if (!existsSync(directory)) {
    throw new IntegrationGateError(`evidence directory does not exist: ${directory}`);
  }
  const directoryStat = lstatSync(directory);
  if (!directoryStat.isDirectory() || directoryStat.isSymbolicLink()) {
    throw new IntegrationGateError("evidence directory must not be a symbolic link");
  }
  return readdirSync(directory)
    .filter((name) => name.endsWith(".integration-evidence.json"))
    .sort();
}

function loadEvidence(directory) {
  return evidenceFiles(directory).map((name) => {
    const manifestPath = path.join(directory, name);
    const stat = lstatSync(manifestPath);
    if (!stat.isFile() || stat.isSymbolicLink()) {
      throw new IntegrationGateError(`evidence manifest must be a regular file: ${name}`);
    }
    const manifestBytes = readFileSync(manifestPath);
    let manifest;
    try {
      manifest = JSON.parse(manifestBytes.toString("utf8"));
    } catch (error) {
      throw new IntegrationGateError(`evidence ${name} is not valid JSON: ${error.message}`);
    }
    if (!manifest || typeof manifest !== "object" || Array.isArray(manifest)) {
      throw new IntegrationGateError(`evidence ${name} must contain a JSON object`);
    }
    const logName = manifest.log?.file;
    if (typeof logName !== "string" || path.basename(logName) !== logName) {
      throw new IntegrationGateError(`evidence ${name} has an unsafe log path`);
    }
    const logPath = path.join(directory, logName);
    if (!existsSync(logPath)) {
      throw new IntegrationGateError(`evidence ${name} is missing log ${logName}`);
    }
    const logStat = lstatSync(logPath);
    if (!logStat.isFile() || logStat.isSymbolicLink()) {
      throw new IntegrationGateError(`evidence log must be a regular file: ${logName}`);
    }
    const log = readFileSync(logPath);
    return {
      name,
      manifest,
      manifestSha256: sha256(manifestBytes),
      logHashValid:
        manifest.log?.sha256 === sha256(log) && manifest.log?.bytes === log.byteLength,
    };
  });
}

export function evaluateEvidence(
  records,
  {
    mode,
    releaseTag,
    commit,
    now = new Date(),
    maxAgeHours = DEFAULT_MAX_EVIDENCE_AGE_HOURS,
    runnerScriptSha256 = sha256(readFileSync(SCRIPT_PATH)),
  },
) {
  if (!Object.hasOwn(REQUIRED_GATES, mode)) {
    throw new IntegrationGateError("mode must be contract, rc, or ga");
  }
  const expectedTag = mode === "ga"
    ? /^v1\.0\.0$/u
    : mode === "rc"
      ? /^v1\.0\.0-rc\.[1-9][0-9]*$/u
      : /^v1\.0\.0(?:-rc\.[1-9][0-9]*)?$/u;
  const failures = [];
  if (!expectedTag.test(releaseTag)) {
    failures.push(`release tag ${releaseTag || "(missing)"} is invalid for ${mode}`);
  }
  if (!/^[0-9a-f]{40}$/u.test(commit)) {
    failures.push("expected commit must be a full 40-character SHA-1");
  }
  if (!Number.isFinite(maxAgeHours) || maxAgeHours <= 0 || maxAgeHours > 31 * 24) {
    failures.push("max evidence age must be between 0 and 744 hours");
  }

  const byGate = new Map();
  for (const record of records) {
    const gateId = String(record.manifest?.gate_id ?? "");
    if (!byGate.has(gateId)) byGate.set(gateId, []);
    byGate.get(gateId).push(record);
  }
  for (const gateId of REQUIRED_GATES[mode]) {
    const matches = byGate.get(gateId) ?? [];
    if (matches.length !== 1) {
      failures.push(`${gateId} requires exactly one evidence record, found ${matches.length}`);
      continue;
    }
    const { manifest, logHashValid, name } = matches[0];
    if (manifest.schema_version !== EVIDENCE_SCHEMA_VERSION) {
      failures.push(`${name}: unsupported schema_version`);
    }
    const expectedDefinition = TARGETS[manifest.target];
    if (!expectedDefinition || expectedDefinition.gateId !== gateId) {
      failures.push(`${name}: target does not match gate_id`);
    } else if (expectedDefinition.kind !== manifest.evidence_kind) {
      failures.push(`${name}: evidence_kind does not match target`);
    }
    if (manifest.status !== "passed") failures.push(`${name}: status is not passed`);
    if (manifest.evidence_eligible !== true) {
      failures.push(`${name}: test passed but evidence is not release-eligible`);
    }
    if (!logHashValid) failures.push(`${name}: log digest or byte count does not match`);
    if (manifest.source?.commit !== commit) failures.push(`${name}: commit does not match release`);
    if (manifest.source?.dirty !== false) failures.push(`${name}: evidence was captured from a dirty worktree`);
    if (manifest.source?.stable_during_run !== true) {
      failures.push(`${name}: source state changed while the gate was running`);
    }
    if (!Array.isArray(manifest.source?.release_tags) || !manifest.source.release_tags.includes(releaseTag)) {
      failures.push(`${name}: evidence is not bound to release tag ${releaseTag}`);
    }
    if (manifest.runner?.automatic_retry !== false) {
      failures.push(`${name}: automatic retry must be explicitly disabled`);
    }
    if (manifest.runner?.script_sha256 !== runnerScriptSha256) {
      failures.push(`${name}: gate runner digest does not match the release runner`);
    }
    const testedAt = Date.parse(String(manifest.tested_at ?? ""));
    const ageHours = (now.getTime() - testedAt) / 3_600_000;
    if (!Number.isFinite(testedAt) || ageHours < 0 || ageHours > maxAgeHours) {
      failures.push(`${name}: evidence is expired or has an invalid timestamp`);
    }
    if (!(manifest.result?.pytest?.passed > 0) || manifest.result?.pytest?.skipped !== 0) {
      failures.push(`${name}: pytest did not record a non-skipped pass`);
    }
    if (manifest.result?.exit_code !== 0 || manifest.result?.timed_out !== false) {
      failures.push(`${name}: runner did not exit cleanly`);
    }
    if (expectedDefinition?.kind === "contract") {
      if (manifest.contract?.real_provider_requests_max !== 0) {
        failures.push(`${name}: credential-free contract gate made or allowed live requests`);
      }
      if (manifest.preflight?.credentialFree !== true) {
        failures.push(`${name}: contract preflight is not marked credential-free`);
      }
    } else if (expectedDefinition?.kind === "real_e2e") {
      if (manifest.preflight?.ready !== true || manifest.preflight?.credentialPresent !== true) {
        failures.push(`${name}: live credential preflight is incomplete`);
      }
    }
    if (gateId === "integration.tencent_docs.real_e2e") {
      if (manifest.contract?.restore_attempted_on_ambiguous_failure !== true) {
        failures.push(`${name}: Tencent restore-on-ambiguous-failure contract is missing`);
      }
    }
    if (gateId === "integration.image_provider.real_e2e") {
      if (manifest.contract?.real_provider_requests_max !== IMAGE_MAX_REQUESTS) {
        failures.push(`${name}: image live gate must be bounded to one provider request`);
      }
      if (manifest.preflight?.automaticRetry !== false) {
        failures.push(`${name}: image preflight must prohibit automatic retry`);
      }
    }
  }
  return { passed: failures.length === 0, failures, requiredGates: REQUIRED_GATES[mode] };
}

export function verifyEvidenceDirectory(
  directory,
  options,
) {
  return evaluateEvidence(loadEvidence(directory), options);
}

export function buildEvidenceSummary(records, options) {
  const evaluation = evaluateEvidence(records, options);
  if (!evaluation.passed) {
    throw new IntegrationGateError(
      `cannot summarize invalid integration evidence:\n${evaluation.failures.join("\n")}`,
    );
  }
  const required = new Set(evaluation.requiredGates);
  const gates = records
    .filter((record) => required.has(record.manifest.gate_id))
    .map((record) => {
      if (!/^[0-9a-f]{64}$/u.test(String(record.manifestSha256 ?? ""))) {
        throw new IntegrationGateError(`${record.name}: manifest SHA-256 is missing`);
      }
      return {
        gate_id: record.manifest.gate_id,
        target: record.manifest.target,
        tested_at: record.manifest.tested_at,
        pytest_passed: record.manifest.result.pytest.passed,
        manifest_file: record.name,
        manifest_sha256: record.manifestSha256,
        log_file: record.manifest.log?.file ?? null,
        log_sha256: record.manifest.log?.sha256 ?? null,
      };
    })
    .sort((left, right) => left.gate_id.localeCompare(right.gate_id));
  return {
    schema_version: EVIDENCE_SCHEMA_VERSION,
    status: "passed",
    mode: options.mode,
    release_tag: options.releaseTag,
    release_commit: options.commit,
    generated_at: (options.now ?? new Date()).toISOString(),
    runner_sha256: options.runnerScriptSha256 ?? sha256(readFileSync(SCRIPT_PATH)),
    gates,
  };
}

export function writeEvidenceSummary(directory, outputPath, options) {
  const summary = buildEvidenceSummary(loadEvidence(directory), options);
  atomicWriteNew(outputPath, `${JSON.stringify(summary, null, 2)}\n`);
  return summary;
}

export function evaluateScorecardIntegrationLink(
  scorecard,
  summary,
  summarySha256,
  { mode },
) {
  const failures = [];
  if (!Object.hasOwn(REQUIRED_GATES, mode) || mode === "contract") {
    throw new IntegrationGateError("scorecard link mode must be rc or ga");
  }
  const link = scorecard.integration_evidence;
  if (!link || typeof link !== "object" || Array.isArray(link)) {
    failures.push("scorecard.integration_evidence is missing");
  } else {
    if (link.summary_sha256 !== summarySha256) {
      failures.push("scorecard integration summary SHA-256 does not match the supplied summary");
    }
    if (link.release_commit !== scorecard.release_commit) {
      failures.push("scorecard integration evidence commit does not match scorecard.release_commit");
    }
    if (link.release_tag !== scorecard.release_tag) {
      failures.push("scorecard integration evidence tag does not match scorecard.release_tag");
    }
  }
  if (summary.schema_version !== EVIDENCE_SCHEMA_VERSION || summary.status !== "passed") {
    failures.push("integration summary is not a passed schema-v1 summary");
  }
  if (summary.mode !== mode) failures.push(`integration summary mode must be ${mode}`);
  if (summary.release_commit !== scorecard.release_commit) {
    failures.push("integration summary commit does not match scorecard.release_commit");
  }
  if (summary.release_tag !== scorecard.release_tag) {
    failures.push("integration summary tag does not match scorecard.release_tag");
  }
  const summaryGates = Array.isArray(summary.gates)
    ? summary.gates.map((gate) => gate?.gate_id)
    : [];
  for (const gateId of REQUIRED_GATES[mode]) {
    if (summaryGates.filter((candidate) => candidate === gateId).length !== 1) {
      failures.push(`integration summary must contain exactly one ${gateId}`);
    }
  }
  const integrations = scorecard.integrations;
  for (const name of ["tencent_docs", "image_provider"]) {
    if (integrations?.[name]?.contract_test !== "passed") {
      failures.push(`scorecard integrations.${name}.contract_test must be passed`);
    }
    if (mode === "ga" && integrations?.[name]?.real_e2e !== "passed") {
      failures.push(`scorecard integrations.${name}.real_e2e must be passed for GA`);
    }
  }
  return { passed: failures.length === 0, failures };
}

export function verifyScorecardIntegrationLink(scorecardPath, summaryPath, options) {
  const scorecard = readJson(scorecardPath, "v1 scorecard");
  const summaryBytes = readFileSync(summaryPath);
  let summary;
  try {
    summary = JSON.parse(summaryBytes.toString("utf8"));
  } catch (error) {
    throw new IntegrationGateError(`integration summary is not valid JSON: ${error.message}`);
  }
  return evaluateScorecardIntegrationLink(
    scorecard,
    summary,
    sha256(summaryBytes),
    options,
  );
}

function parseCli(args) {
  const positionals = [];
  const options = {};
  for (let index = 0; index < args.length; index += 1) {
    const item = args[index];
    if (!item.startsWith("--")) {
      positionals.push(item);
      continue;
    }
    const name = item.slice(2);
    const value = args[index + 1];
    if (!value || value.startsWith("--")) {
      throw new IntegrationGateError(`--${name} requires a value`);
    }
    options[name] = value;
    index += 1;
  }
  return { positionals, options };
}

function usage() {
  return [
    "Usage:",
    "  node scripts/v1-real-integration-gates.mjs contract [--evidence-dir DIR] [--require-evidence-eligible true]",
    "  node scripts/v1-real-integration-gates.mjs preflight <tencent-real-write|siliconflow-image-real>",
    "  node scripts/v1-real-integration-gates.mjs live <tencent-real-write|siliconflow-image-real> [--evidence-dir DIR]",
    "  node scripts/v1-real-integration-gates.mjs verify --mode <contract|rc|ga> --release-tag TAG [--commit SHA] [--evidence-dir DIR] [--max-age-hours N]",
    "  node scripts/v1-real-integration-gates.mjs summarize --mode <contract|rc|ga> --release-tag TAG --output FILE [--commit SHA] [--evidence-dir DIR]",
    "  node scripts/v1-real-integration-gates.mjs verify-scorecard-link --mode <rc|ga> --scorecard FILE --summary FILE",
  ].join("\n");
}

function main(args) {
  const [command, ...rest] = args;
  const parsed = parseCli(rest);
  const evidenceDirectory = path.resolve(parsed.options["evidence-dir"] || DEFAULT_EVIDENCE_DIR);
  if (command === "preflight") {
    const target = parsed.positionals[0];
    process.stdout.write(`${JSON.stringify(preflightTarget(target), null, 2)}\n`);
    return 0;
  }
  if (command === "contract") {
    const requireEligible = parsed.options["require-evidence-eligible"] === "true";
    if (
      parsed.options["require-evidence-eligible"] !== undefined &&
      !["true", "false"].includes(parsed.options["require-evidence-eligible"])
    ) {
      throw new IntegrationGateError("--require-evidence-eligible must be true or false");
    }
    let testsFailed = false;
    let evidenceIneligible = false;
    for (const target of ["tencent-contract", "siliconflow-image-contract"]) {
      const result = runTarget(target, { evidenceDirectory });
      const outcome = result.testPassed
        ? result.evidenceEligible
          ? "TEST PASSED / EVIDENCE ELIGIBLE"
          : "TEST PASSED / EVIDENCE INELIGIBLE"
        : "TEST FAILED / EVIDENCE INELIGIBLE";
      process.stdout.write(
        `${target}: ${outcome} (${result.manifestPath})\n`,
      );
      if (!result.evidenceEligible) {
        for (const failure of result.manifest.evidence_eligibility_failures) {
          process.stdout.write(`  - ${failure}\n`);
        }
      }
      testsFailed ||= !result.testPassed;
      evidenceIneligible ||= !result.evidenceEligible;
    }
    return testsFailed || (requireEligible && evidenceIneligible) ? 1 : 0;
  }
  if (command === "live") {
    const target = parsed.positionals[0];
    if (!TARGETS[target] || TARGETS[target].kind !== "real_e2e") {
      throw new IntegrationGateError("live requires tencent-real-write or siliconflow-image-real");
    }
    const result = runTarget(target, { evidenceDirectory });
    const outcome = result.testPassed
      ? result.evidenceEligible
        ? "TEST PASSED / EVIDENCE ELIGIBLE"
        : "TEST PASSED / EVIDENCE INELIGIBLE"
      : "TEST FAILED / EVIDENCE INELIGIBLE";
    process.stdout.write(
      `${target}: ${outcome} (${result.manifestPath})\n`,
    );
    if (!result.evidenceEligible) {
      for (const failure of result.manifest.evidence_eligibility_failures) {
        process.stdout.write(`  - ${failure}\n`);
      }
    }
    return result.testPassed && result.evidenceEligible ? 0 : 1;
  }
  if (command === "verify") {
    const mode = parsed.options.mode;
    const releaseTag = parsed.options["release-tag"];
    const commit = parsed.options.commit || git(["rev-parse", "HEAD"]);
    const maxAgeHours = parsed.options["max-age-hours"] === undefined
      ? DEFAULT_MAX_EVIDENCE_AGE_HOURS
      : Number(parsed.options["max-age-hours"]);
    const result = verifyEvidenceDirectory(evidenceDirectory, {
      mode,
      releaseTag,
      commit,
      maxAgeHours,
    });
    if (result.passed) {
      process.stdout.write(
        `PASS: ${mode.toUpperCase()} real-integration evidence is complete for ${releaseTag} at ${commit}.\n`,
      );
      return 0;
    }
    process.stderr.write("FAIL: real-integration evidence is incomplete or invalid:\n");
    for (const failure of result.failures) process.stderr.write(`- ${failure}\n`);
    return 1;
  }
  if (command === "summarize") {
    const mode = parsed.options.mode;
    const releaseTag = parsed.options["release-tag"];
    const commit = parsed.options.commit || git(["rev-parse", "HEAD"]);
    const output = parsed.options.output;
    if (!output) throw new IntegrationGateError("summarize requires --output FILE");
    const maxAgeHours = parsed.options["max-age-hours"] === undefined
      ? DEFAULT_MAX_EVIDENCE_AGE_HOURS
      : Number(parsed.options["max-age-hours"]);
    const summary = writeEvidenceSummary(evidenceDirectory, path.resolve(output), {
      mode,
      releaseTag,
      commit,
      maxAgeHours,
    });
    process.stdout.write(
      `PASS: wrote ${summary.gates.length} verified integration gates to ${path.resolve(output)}.\n`,
    );
    return 0;
  }
  if (command === "verify-scorecard-link") {
    const mode = parsed.options.mode;
    const scorecard = parsed.options.scorecard;
    const summary = parsed.options.summary;
    if (!scorecard || !summary) {
      throw new IntegrationGateError(
        "verify-scorecard-link requires --scorecard FILE and --summary FILE",
      );
    }
    const result = verifyScorecardIntegrationLink(
      path.resolve(scorecard),
      path.resolve(summary),
      { mode },
    );
    if (result.passed) {
      process.stdout.write("PASS: scorecard integration evidence link is valid.\n");
      return 0;
    }
    process.stderr.write("FAIL: scorecard integration evidence link is invalid:\n");
    for (const failure of result.failures) process.stderr.write(`- ${failure}\n`);
    return 1;
  }
  throw new IntegrationGateError(usage());
}

if (process.argv[1] && path.resolve(process.argv[1]) === path.resolve(SCRIPT_PATH)) {
  try {
    process.exitCode = main(process.argv.slice(2));
  } catch (error) {
    process.stderr.write(`ERROR: ${error.message}\n${usage()}\n`);
    process.exitCode = 2;
  }
}
