#!/usr/bin/env node

import { createHash } from "node:crypto";
import {
  lstatSync,
  linkSync,
  mkdirSync,
  readFileSync,
  readdirSync,
  unlinkSync,
  writeFileSync,
} from "node:fs";
import { dirname, join, resolve } from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";

export const ACP_SOAK_SCHEMA_VERSION = 1;
export const ACP_SOAK_CONTRACT_VERSION = "v1.1-acp-stdio-soak-1";
export const ACP_SOAK_SDK_VERSION = "0.10.1";
export const ACP_SOAK_MINIMUM_MILLISECONDS = 8 * 60 * 60 * 1000;
export const ACP_SOAK_REPORT_FILENAME = "acp-soak-report.json";

export const REQUIRED_ACP_SOAK_COVERAGE = Object.freeze([
  "initialize",
  "session_new",
  "session_load",
  "prompt",
  "message_updates",
  "plan_updates",
  "tool_updates",
  "permission_allow_once",
  "permission_deny",
  "cancel",
  "disconnect_fail_closed",
  "resume",
  "cjk_workspace",
]);

const REQUIRED_POSITIVE_COUNTS = Object.freeze({
  sessions_created: 10,
  sessions_loaded: 10,
  prompts_completed: 100,
  permission_requests: 20,
  cancellations: 20,
  disconnects: 20,
  cjk_workspace_runs: 20,
  agent_uptime_seconds: 8 * 60 * 60,
});
const REQUIRED_ZERO_COUNTS = Object.freeze([
  "duplicate_writes",
  "unfinished_journals",
  "orphan_processes",
  "cross_session_events",
  "protocol_errors",
]);
const SUPPORTED_PLATFORMS = new Set([
  "windows-x64",
  "macos-arm64",
  "macos-x64",
  "linux-x64",
  "linux-arm64",
]);
const RELEASE_REF_PATTERN = /^v1\.1\.0(?:-rc\.[1-9][0-9]*)?$/u;
const COMMIT_PATTERN = /^(?!0{40}$)[0-9a-f]{40}$/u;
const SHA256_PATTERN = /^(?!0{64}$)[0-9a-f]{64}$/u;
const CLIENT_ID_PATTERN = /^[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?$/u;
const RUN_ID_PATTERN = /^[A-Za-z0-9](?:[A-Za-z0-9._:-]{0,126}[A-Za-z0-9])?$/u;
const MAX_REPORT_BYTES = 1024 * 1024;

export class AcpSoakEvidenceError extends Error {}

function sha256(bytes) {
  return createHash("sha256").update(bytes).digest("hex");
}

function record(value, label) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new AcpSoakEvidenceError(`${label} must be an object`);
  }
  return value;
}

function exactKeys(value, expected, label) {
  const actual = Object.keys(value).sort();
  const wanted = [...expected].sort();
  if (actual.length !== wanted.length || actual.some((key, index) => key !== wanted[index])) {
    throw new AcpSoakEvidenceError(
      `${label} fields must be exactly: ${wanted.join(", ")}`,
    );
  }
}

function requiredString(value, label, { pattern, maxLength = 256 } = {}) {
  if (
    typeof value !== "string" ||
    value.length === 0 ||
    value !== value.trim() ||
    value.length > maxLength ||
    /[\u0000-\u001f\u007f]/u.test(value) ||
    (pattern && !pattern.test(value))
  ) {
    throw new AcpSoakEvidenceError(`${label} is invalid`);
  }
  return value;
}

function nonnegativeInteger(value, label) {
  if (!Number.isSafeInteger(value) || value < 0) {
    throw new AcpSoakEvidenceError(`${label} must be a non-negative safe integer`);
  }
  return value;
}

function canonicalTimestamp(value, label) {
  const text = requiredString(value, label, { maxLength: 64 });
  const milliseconds = Date.parse(text);
  if (
    !Number.isFinite(milliseconds) ||
    !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$/u.test(text) ||
    new Date(milliseconds).toISOString() !== text
  ) {
    throw new AcpSoakEvidenceError(`${label} must be a canonical UTC timestamp`);
  }
  return { text, milliseconds };
}

function validateClient(value) {
  const client = record(value, "client");
  exactKeys(
    client,
    ["id", "name", "version", "executable_sha256", "provenance_sha256"],
    "client",
  );
  const normalized = {
    id: requiredString(client.id, "client.id", {
      pattern: CLIENT_ID_PATTERN,
      maxLength: 64,
    }),
    name: requiredString(client.name, "client.name", { maxLength: 128 }),
    version: requiredString(client.version, "client.version", { maxLength: 64 }),
    executable_sha256: requiredString(
      client.executable_sha256,
      "client.executable_sha256",
      { pattern: SHA256_PATTERN, maxLength: 64 },
    ),
    provenance_sha256: requiredString(
      client.provenance_sha256,
      "client.provenance_sha256",
      { pattern: SHA256_PATTERN, maxLength: 64 },
    ),
  };
  if (
    /[/\\]/u.test(normalized.name) ||
    normalized.name.includes("..") ||
    /[/\\]/u.test(normalized.version) ||
    normalized.version.includes("..")
  ) {
    throw new AcpSoakEvidenceError("client name and version must be path-free");
  }
  return normalized;
}

function validateAgent(value, expectedBinaryIdentitySha256) {
  const agent = record(value, "agent");
  exactKeys(
    agent,
    [
      "binary_sha256",
      "binary_identity_manifest_sha256",
      "frozen_backend",
      "protocol_version",
      "sdk_name",
      "sdk_version",
    ],
    "agent",
  );
  if (agent.frozen_backend !== true) {
    throw new AcpSoakEvidenceError("agent.frozen_backend must be true");
  }
  if (agent.protocol_version !== 1) {
    throw new AcpSoakEvidenceError("agent.protocol_version must be 1");
  }
  if (agent.sdk_name !== "agent-client-protocol") {
    throw new AcpSoakEvidenceError(
      "agent.sdk_name must be agent-client-protocol",
    );
  }
  const binaryIdentityManifestSha256 = requiredString(
    agent.binary_identity_manifest_sha256,
    "agent.binary_identity_manifest_sha256",
    { pattern: SHA256_PATTERN, maxLength: 64 },
  );
  if (
    expectedBinaryIdentitySha256 &&
    binaryIdentityManifestSha256 !== expectedBinaryIdentitySha256
  ) {
    throw new AcpSoakEvidenceError(
      "agent binary identity does not match the release matrix",
    );
  }
  return {
    binary_sha256: requiredString(agent.binary_sha256, "agent.binary_sha256", {
      pattern: SHA256_PATTERN,
      maxLength: 64,
    }),
    binary_identity_manifest_sha256: binaryIdentityManifestSha256,
    frozen_backend: true,
    protocol_version: 1,
    sdk_name: agent.sdk_name,
    sdk_version: (() => {
      const version = requiredString(agent.sdk_version, "agent.sdk_version", {
        maxLength: 64,
      });
      if (version !== ACP_SOAK_SDK_VERSION) {
        throw new AcpSoakEvidenceError(
          `agent.sdk_version must be ${ACP_SOAK_SDK_VERSION}`,
        );
      }
      return version;
    })(),
  };
}

function validateCoverage(value) {
  const coverage = record(value, "coverage");
  exactKeys(coverage, REQUIRED_ACP_SOAK_COVERAGE, "coverage");
  for (const field of REQUIRED_ACP_SOAK_COVERAGE) {
    if (coverage[field] !== true) {
      throw new AcpSoakEvidenceError(`coverage.${field} must be true`);
    }
  }
  return Object.fromEntries(REQUIRED_ACP_SOAK_COVERAGE.map((field) => [field, true]));
}

function validateCounts(value) {
  const counts = record(value, "counts");
  const fields = [...Object.keys(REQUIRED_POSITIVE_COUNTS), ...REQUIRED_ZERO_COUNTS];
  exactKeys(counts, fields, "counts");
  const normalized = {};
  for (const [field, minimum] of Object.entries(REQUIRED_POSITIVE_COUNTS)) {
    const count = nonnegativeInteger(counts[field], `counts.${field}`);
    if (count < minimum) {
      throw new AcpSoakEvidenceError(`counts.${field} must be at least ${minimum}`);
    }
    normalized[field] = count;
  }
  for (const field of REQUIRED_ZERO_COUNTS) {
    const count = nonnegativeInteger(counts[field], `counts.${field}`);
    if (count !== 0) {
      throw new AcpSoakEvidenceError(`counts.${field} must be zero`);
    }
    normalized[field] = 0;
  }
  return normalized;
}

function validatePrivacy(value) {
  const privacy = record(value, "privacy");
  exactKeys(
    privacy,
    ["prompts_collected", "file_paths_collected", "secrets_collected"],
    "privacy",
  );
  for (const field of ["prompts_collected", "file_paths_collected", "secrets_collected"]) {
    if (privacy[field] !== false) {
      throw new AcpSoakEvidenceError(`privacy.${field} must be false`);
    }
  }
  return {
    prompts_collected: false,
    file_paths_collected: false,
    secrets_collected: false,
  };
}

export function validateAcpSoakReport(
  value,
  { expectedCommit, expectedReleaseRef, expectedAgentBinaryIdentitySha256 } = {},
) {
  const report = record(value, "ACP soak report");
  exactKeys(
    report,
    [
      "schema_version",
      "contract_version",
      "status",
      "run_id",
      "source_commit",
      "release_ref",
      "platform",
      "started_at",
      "completed_at",
      "client",
      "agent",
      "coverage",
      "counts",
      "privacy",
    ],
    "ACP soak report",
  );
  if (report.schema_version !== ACP_SOAK_SCHEMA_VERSION) {
    throw new AcpSoakEvidenceError("schema_version must be 1");
  }
  if (report.contract_version !== ACP_SOAK_CONTRACT_VERSION) {
    throw new AcpSoakEvidenceError(
      `contract_version must be ${ACP_SOAK_CONTRACT_VERSION}`,
    );
  }
  if (report.status !== "ok") {
    throw new AcpSoakEvidenceError("status must be ok");
  }
  const sourceCommit = requiredString(report.source_commit, "source_commit", {
    pattern: COMMIT_PATTERN,
    maxLength: 40,
  });
  const releaseRef = requiredString(report.release_ref, "release_ref", {
    pattern: RELEASE_REF_PATTERN,
    maxLength: 64,
  });
  if (expectedCommit && sourceCommit !== String(expectedCommit).toLowerCase()) {
    throw new AcpSoakEvidenceError("source_commit does not match the release commit");
  }
  if (expectedReleaseRef && releaseRef !== expectedReleaseRef) {
    throw new AcpSoakEvidenceError("release_ref does not match the release ref");
  }
  const platform = requiredString(report.platform, "platform", { maxLength: 32 });
  if (!SUPPORTED_PLATFORMS.has(platform)) {
    throw new AcpSoakEvidenceError("platform is not a v1.1 native target");
  }
  const started = canonicalTimestamp(report.started_at, "started_at");
  const completed = canonicalTimestamp(report.completed_at, "completed_at");
  const durationMilliseconds = completed.milliseconds - started.milliseconds;
  if (durationMilliseconds < ACP_SOAK_MINIMUM_MILLISECONDS) {
    throw new AcpSoakEvidenceError("ACP soak duration must be at least 8 hours");
  }
  const counts = validateCounts(report.counts);
  if (counts.agent_uptime_seconds * 1000 < durationMilliseconds) {
    throw new AcpSoakEvidenceError(
      "counts.agent_uptime_seconds must cover the complete soak interval",
    );
  }
  return {
    schema_version: 1,
    contract_version: ACP_SOAK_CONTRACT_VERSION,
    status: "ok",
    run_id: requiredString(report.run_id, "run_id", {
      pattern: RUN_ID_PATTERN,
      maxLength: 128,
    }),
    source_commit: sourceCommit,
    release_ref: releaseRef,
    platform,
    started_at: started.text,
    completed_at: completed.text,
    duration_milliseconds: durationMilliseconds,
    client: validateClient(report.client),
    agent: validateAgent(report.agent, expectedAgentBinaryIdentitySha256),
    coverage: validateCoverage(report.coverage),
    counts,
    privacy: validatePrivacy(report.privacy),
  };
}

export function aggregateAcpSoakEvidence(
  reports,
  { expectedCommit, expectedReleaseRef, expectedAgentBinaryIdentitySha256 } = {},
) {
  const commit = requiredString(String(expectedCommit ?? "").toLowerCase(), "expectedCommit", {
    pattern: COMMIT_PATTERN,
    maxLength: 40,
  });
  const releaseRef = requiredString(expectedReleaseRef, "expectedReleaseRef", {
    pattern: RELEASE_REF_PATTERN,
    maxLength: 64,
  });
  const agentBinaryIdentitySha256 = requiredString(
    expectedAgentBinaryIdentitySha256,
    "expectedAgentBinaryIdentitySha256",
    { pattern: SHA256_PATTERN, maxLength: 64 },
  );
  if (!Array.isArray(reports) || reports.length !== 2) {
    throw new AcpSoakEvidenceError("exactly two ACP client soak reports are required");
  }
  const normalized = reports.map((item, index) => {
    if (!(typeof item?.raw === "string" || Buffer.isBuffer(item?.raw))) {
      throw new AcpSoakEvidenceError(
        `report ${index + 1} must provide the original report bytes`,
      );
    }
    const bytes = Buffer.isBuffer(item.raw) ? item.raw : Buffer.from(item.raw, "utf8");
    if (bytes.length < 1 || bytes.length > MAX_REPORT_BYTES) {
      throw new AcpSoakEvidenceError(`report ${index + 1} has an invalid byte length`);
    }
    let raw;
    let value;
    try {
      raw = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
      value = JSON.parse(raw);
    } catch {
      throw new AcpSoakEvidenceError(`report ${index + 1} is not valid UTF-8 JSON`);
    }
    if (raw !== `${JSON.stringify(value)}\n`) {
      throw new AcpSoakEvidenceError(
        `report ${index + 1} must use canonical single-line JSON bytes`,
      );
    }
    const report = validateAcpSoakReport(value, {
      expectedCommit: commit,
      expectedReleaseRef: releaseRef,
      expectedAgentBinaryIdentitySha256: agentBinaryIdentitySha256,
    });
    return {
      ...report,
      report_sha256: sha256(bytes),
    };
  });
  const ids = new Set(normalized.map((item) => item.client.id));
  const executableDigests = new Set(
    normalized.map((item) => item.client.executable_sha256),
  );
  const provenanceDigests = new Set(
    normalized.map((item) => item.client.provenance_sha256),
  );
  const runIds = new Set(normalized.map((item) => item.run_id));
  if (ids.size !== 2 || executableDigests.size !== 2 || provenanceDigests.size !== 2) {
    throw new AcpSoakEvidenceError(
      "ACP evidence must come from two distinct client implementations and builds",
    );
  }
  if (runIds.size !== 2) {
    throw new AcpSoakEvidenceError("ACP soak run_id values must be distinct");
  }
  normalized.sort((left, right) => left.client.id.localeCompare(right.client.id));
  return {
    schema_version: 1,
    contract_version: ACP_SOAK_CONTRACT_VERSION,
    status: "ok",
    source_commit: commit,
    release_ref: releaseRef,
    agent_binary_identity_sha256: agentBinaryIdentitySha256,
    client_count: 2,
    minimum_duration_milliseconds: Math.min(
      ...normalized.map((item) => item.duration_milliseconds),
    ),
    clients: normalized,
  };
}

function readStableReport(pathname) {
  const path = resolve(pathname);
  const before = lstatSync(path);
  if (!before.isFile() || before.isSymbolicLink() || before.size <= 0 || before.size > MAX_REPORT_BYTES) {
    throw new AcpSoakEvidenceError("ACP soak report must be a bounded regular file");
  }
  const bytes = readFileSync(path);
  const after = lstatSync(path);
  if (
    !after.isFile() ||
    after.isSymbolicLink() ||
    before.dev !== after.dev ||
    before.ino !== after.ino ||
    before.size !== after.size ||
    before.mtimeMs !== after.mtimeMs ||
    before.ctimeMs !== after.ctimeMs ||
    bytes.length !== after.size
  ) {
    throw new AcpSoakEvidenceError("ACP soak report changed while it was read");
  }
  let raw;
  try {
    raw = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
    JSON.parse(raw);
  } catch {
    throw new AcpSoakEvidenceError("ACP soak report is not valid UTF-8 JSON");
  }
  return { raw };
}

function findReports(root) {
  const rootPath = resolve(root);
  const rootInfo = lstatSync(rootPath);
  if (!rootInfo.isDirectory() || rootInfo.isSymbolicLink()) {
    throw new AcpSoakEvidenceError(
      "ACP soak reports root must be a regular directory",
    );
  }
  const reports = [];
  const visit = (directory) => {
    for (const entry of readdirSync(directory, { withFileTypes: true })) {
      const path = join(directory, entry.name);
      if (entry.isSymbolicLink()) {
        throw new AcpSoakEvidenceError(
          "ACP soak evidence tree must not contain symbolic links",
        );
      } else if (entry.isDirectory()) {
        visit(path);
      } else if (entry.isFile() && entry.name === ACP_SOAK_REPORT_FILENAME) {
        reports.push(readStableReport(path));
      }
    }
  };
  visit(rootPath);
  return reports;
}

function writeNewOutput(pathname, value) {
  const destination = resolve(pathname);
  mkdirSync(dirname(destination), { recursive: true });
  try {
    lstatSync(destination);
    throw new AcpSoakEvidenceError("ACP soak summary output already exists");
  } catch (error) {
    if (!(error instanceof AcpSoakEvidenceError) && error?.code !== "ENOENT") {
      throw error;
    }
    if (error instanceof AcpSoakEvidenceError) throw error;
  }
  const temporary = `${destination}.tmp-${process.pid}`;
  try {
    writeFileSync(temporary, `${JSON.stringify(value, null, 2)}\n`, {
      encoding: "utf8",
      mode: 0o600,
      flag: "wx",
    });
    linkSync(temporary, destination);
    unlinkSync(temporary);
  } catch (error) {
    try {
      unlinkSync(temporary);
    } catch {}
    throw error;
  }
}

function main(argv) {
  if (argv.length !== 6 || argv[0] !== "aggregate") {
    throw new AcpSoakEvidenceError(
      "usage: v11-acp-soak-evidence.mjs aggregate <reports-dir> <commit> " +
        "<release-ref> <agent-binary-identity-sha256> <output>",
    );
  }
  const [
    ,
    reportsDirectory,
    commit,
    releaseRef,
    agentBinaryIdentitySha256,
    output,
  ] = argv;
  const summary = aggregateAcpSoakEvidence(findReports(reportsDirectory), {
    expectedCommit: commit,
    expectedReleaseRef: releaseRef,
    expectedAgentBinaryIdentitySha256: agentBinaryIdentitySha256,
  });
  writeNewOutput(output, summary);
  process.stdout.write(
    `[v11-acp-soak-evidence] verified ${summary.client_count} clients; minimum soak ${
      summary.minimum_duration_milliseconds / 3_600_000
    }h\n`,
  );
}

if (resolve(process.argv[1] ?? "") === fileURLToPath(import.meta.url)) {
  try {
    main(process.argv.slice(2));
  } catch (error) {
    process.stderr.write(`[v11-acp-soak-evidence] ${error.message}\n`);
    process.exitCode = 1;
  }
}
