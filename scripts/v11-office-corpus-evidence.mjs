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

export const OFFICE_CORPUS_SCHEMA_VERSION = 1;
export const OFFICE_CORPUS_CONTRACT_VERSION = "v1.1-office-corpus-1";
export const OFFICE_CORPUS_REPORT_FILENAME = "v11-office-corpus-report.json";
export const OFFICE_CORPUS_CASE_COUNT = 300;
export const OFFICE_CORPUS_MINIMUM_PER_FORMAT = 100;
export const OFFICE_CORPUS_MINIMUM_PER_PRIMARY_BUCKET = 20;
export const REQUIRED_OFFICE_CORPUS_TARGETS = Object.freeze([
  "windows-x64",
  "windows-arm64",
  "macos-arm64",
  "macos-x64",
  "linux-x64",
  "linux-arm64",
]);
export const REQUIRED_OFFICE_CORPUS_FORMATS = Object.freeze([
  "docx",
  "xlsx",
  "pptx",
]);
export const REQUIRED_OFFICE_CORPUS_PRIMARY_BUCKETS = Object.freeze([
  "cjk",
  "complex",
  "edit-rewind",
  "unsupported",
]);

const RELEASE_REF_PATTERN =
  /^v1\.1\.(?:0|[1-9][0-9]*)(?:-rc\.[1-9][0-9]*)?$/u;
const COMMIT_PATTERN = /^(?!0{40}$)[0-9a-f]{40}$/u;
const SHA256_PATTERN = /^(?!0{64}$)[0-9a-f]{64}$/u;
const ID_PATTERN = /^[a-z0-9](?:[a-z0-9._:-]{0,94}[a-z0-9])?$/u;
const VERSION_PATTERN = /^[A-Za-z0-9](?:[A-Za-z0-9.+_-]{0,94}[A-Za-z0-9])?$/u;
const MAX_REPORT_BYTES = 32 * 1024 * 1024;
const MAX_RENDERED_PAGES = 512;

export class OfficeCorpusEvidenceError extends Error {}

function sha256(bytes) {
  return createHash("sha256").update(bytes).digest("hex");
}

function record(value, label) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new OfficeCorpusEvidenceError(`${label} must be an object`);
  }
  return value;
}

function exactKeys(value, expected, label) {
  const actual = Object.keys(value).sort();
  const wanted = [...expected].sort();
  if (
    actual.length !== wanted.length ||
    actual.some((key, index) => key !== wanted[index])
  ) {
    throw new OfficeCorpusEvidenceError(
      `${label} fields must be exactly: ${wanted.join(", ")}`,
    );
  }
}

function requiredString(
  value,
  label,
  { pattern = ID_PATTERN, maxLength = 96 } = {},
) {
  if (
    typeof value !== "string" ||
    value.length === 0 ||
    value !== value.trim() ||
    value.length > maxLength ||
    /[\u0000-\u001f\u007f/\\]/u.test(value) ||
    value.includes("..") ||
    (pattern && !pattern.test(value))
  ) {
    throw new OfficeCorpusEvidenceError(`${label} is invalid`);
  }
  return value;
}

function digest(value, label) {
  return requiredString(value, label, {
    pattern: SHA256_PATTERN,
    maxLength: 64,
  });
}

function commitId(value, label) {
  return requiredString(value, label, {
    pattern: COMMIT_PATTERN,
    maxLength: 40,
  });
}

function releaseRef(value, label) {
  return requiredString(value, label, {
    pattern: RELEASE_REF_PATTERN,
    maxLength: 64,
  });
}

function canonicalTimestamp(value, label) {
  const text = requiredString(value, label, {
    pattern: /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$/u,
    maxLength: 24,
  });
  const milliseconds = Date.parse(text);
  if (!Number.isFinite(milliseconds) || new Date(milliseconds).toISOString() !== text) {
    throw new OfficeCorpusEvidenceError(
      `${label} must be a canonical UTC timestamp`,
    );
  }
  return { text, milliseconds };
}

function safePositiveInteger(value, label) {
  if (!Number.isSafeInteger(value) || value < 1) {
    throw new OfficeCorpusEvidenceError(`${label} must be a positive safe integer`);
  }
  return value;
}

function validatePrivacy(value) {
  const privacy = record(value, "privacy");
  exactKeys(
    privacy,
    [
      "document_content_collected",
      "file_paths_collected",
      "rendered_bytes_collected",
    ],
    "privacy",
  );
  for (const field of [
    "document_content_collected",
    "file_paths_collected",
    "rendered_bytes_collected",
  ]) {
    if (privacy[field] !== false) {
      throw new OfficeCorpusEvidenceError(`privacy.${field} must be false`);
    }
  }
  return {
    document_content_collected: false,
    file_paths_collected: false,
    rendered_bytes_collected: false,
  };
}

function validateRenderEnvironment(value, expected = {}) {
  const environment = record(value, "render_environment");
  exactKeys(environment, ["renderer", "fonts", "parameters"], "render_environment");

  const renderer = record(environment.renderer, "render_environment.renderer");
  exactKeys(
    renderer,
    [
      "authoritative",
      "id",
      "version",
      "identity_manifest_sha256",
      "executable_sha256",
      "bundle_tree_sha256",
      "attestation_sha256",
    ],
    "render_environment.renderer",
  );
  if (renderer.authoritative !== true) {
    throw new OfficeCorpusEvidenceError(
      "render_environment.renderer.authoritative must be true",
    );
  }
  const attestationSha256 = digest(
    renderer.attestation_sha256,
    "render_environment.renderer.attestation_sha256",
  );
  const rendererVersion = requiredString(
    renderer.version,
    "render_environment.renderer.version",
    {
      pattern: VERSION_PATTERN,
      maxLength: 96,
    },
  );
  const normalizedRenderer = {
    authoritative: true,
    id: requiredString(renderer.id, "render_environment.renderer.id"),
    version: rendererVersion,
    identity_manifest_sha256: digest(
      renderer.identity_manifest_sha256,
      "render_environment.renderer.identity_manifest_sha256",
    ),
    executable_sha256: digest(
      renderer.executable_sha256,
      "render_environment.renderer.executable_sha256",
    ),
    bundle_tree_sha256: digest(
      renderer.bundle_tree_sha256,
      "render_environment.renderer.bundle_tree_sha256",
    ),
    attestation_sha256: attestationSha256,
  };
  if (
    normalizedRenderer.id !== "suxiaoyou-attested-office" ||
    normalizedRenderer.version !== `attestation-${attestationSha256}`
  ) {
    throw new OfficeCorpusEvidenceError(
      "render_environment.renderer must match the attested runtime identity",
    );
  }

  const fonts = record(environment.fonts, "render_environment.fonts");
  exactKeys(fonts, ["set_id", "manifest_sha256"], "render_environment.fonts");
  const normalizedFonts = {
    set_id: requiredString(fonts.set_id, "render_environment.fonts.set_id"),
    manifest_sha256: digest(
      fonts.manifest_sha256,
      "render_environment.fonts.manifest_sha256",
    ),
  };

  const parameters = record(environment.parameters, "render_environment.parameters");
  exactKeys(
    parameters,
    ["profile_id", "manifest_sha256"],
    "render_environment.parameters",
  );
  const normalizedParameters = {
    profile_id: requiredString(
      parameters.profile_id,
      "render_environment.parameters.profile_id",
    ),
    manifest_sha256: digest(
      parameters.manifest_sha256,
      "render_environment.parameters.manifest_sha256",
    ),
  };

  const expectedBindings = [
    [
      normalizedRenderer.identity_manifest_sha256,
      expected.rendererIdentitySha256,
      "renderer identity manifest",
    ],
    [normalizedFonts.manifest_sha256, expected.fontsManifestSha256, "font manifest"],
    [
      normalizedParameters.manifest_sha256,
      expected.parametersManifestSha256,
      "render parameters manifest",
    ],
  ];
  for (const [actual, wanted, label] of expectedBindings) {
    if (wanted && actual !== wanted) {
      throw new OfficeCorpusEvidenceError(`${label} does not match the release identity`);
    }
  }
  return {
    renderer: normalizedRenderer,
    fonts: normalizedFonts,
    parameters: normalizedParameters,
  };
}

function validateBooleanProof(value, label, extraFields = []) {
  const proof = record(value, label);
  exactKeys(proof, ["passed", "report_sha256", ...extraFields], label);
  if (proof.passed !== true) {
    throw new OfficeCorpusEvidenceError(`${label}.passed must be true`);
  }
  return proof;
}

function validatePixelHashes(value, label) {
  if (!Array.isArray(value) || value.length < 1 || value.length > MAX_RENDERED_PAGES) {
    throw new OfficeCorpusEvidenceError(
      `${label} must contain between 1 and ${MAX_RENDERED_PAGES} page hashes`,
    );
  }
  return value.map((item, index) => digest(item, `${label}[${index}]`));
}

function validateSupportedCase(value, common, environment) {
  exactKeys(
    value,
    [
      "case_id",
      "format",
      "primary_bucket",
      "outcome",
      "source_sha256",
      "final_sha256",
      "commit_count",
      "seal",
      "independent_reopen",
      "render",
      "structure",
      "rewind",
    ],
    `supported case ${common.case_id}`,
  );
  if (common.primary_bucket === "unsupported") {
    throw new OfficeCorpusEvidenceError(
      `case ${common.case_id} unsupported bucket must use unsupported outcome`,
    );
  }
  const finalSha256 = digest(value.final_sha256, `case ${common.case_id}.final_sha256`);
  if (finalSha256 === common.source_sha256) {
    throw new OfficeCorpusEvidenceError(
      `case ${common.case_id} supported artifact must differ from its source`,
    );
  }
  const commitCount = safePositiveInteger(
    value.commit_count,
    `case ${common.case_id}.commit_count`,
  );

  const seal = validateBooleanProof(value.seal, `case ${common.case_id}.seal`, [
    "artifact_sha256",
  ]);
  const normalizedSeal = {
    passed: true,
    report_sha256: digest(
      seal.report_sha256,
      `case ${common.case_id}.seal.report_sha256`,
    ),
    artifact_sha256: digest(
      seal.artifact_sha256,
      `case ${common.case_id}.seal.artifact_sha256`,
    ),
  };
  if (normalizedSeal.artifact_sha256 !== finalSha256) {
    throw new OfficeCorpusEvidenceError(
      `case ${common.case_id} seal is not bound to the final artifact`,
    );
  }

  const reopen = validateBooleanProof(
    value.independent_reopen,
    `case ${common.case_id}.independent_reopen`,
    ["artifact_sha256"],
  );
  const normalizedReopen = {
    passed: true,
    report_sha256: digest(
      reopen.report_sha256,
      `case ${common.case_id}.independent_reopen.report_sha256`,
    ),
    artifact_sha256: digest(
      reopen.artifact_sha256,
      `case ${common.case_id}.independent_reopen.artifact_sha256`,
    ),
  };
  if (normalizedReopen.artifact_sha256 !== finalSha256) {
    throw new OfficeCorpusEvidenceError(
      `case ${common.case_id} independent reopen is not bound to the final artifact`,
    );
  }

  const render = record(value.render, `case ${common.case_id}.render`);
  exactKeys(
    render,
    ["artifact_sha256", "parameters_manifest_sha256", "runs"],
    `case ${common.case_id}.render`,
  );
  const renderArtifactSha256 = digest(
    render.artifact_sha256,
    `case ${common.case_id}.render.artifact_sha256`,
  );
  if (renderArtifactSha256 !== finalSha256) {
    throw new OfficeCorpusEvidenceError(
      `case ${common.case_id} render is not bound to the final artifact`,
    );
  }
  const renderParametersSha256 = digest(
    render.parameters_manifest_sha256,
    `case ${common.case_id}.render.parameters_manifest_sha256`,
  );
  if (renderParametersSha256 !== environment.parameters.manifest_sha256) {
    throw new OfficeCorpusEvidenceError(
      `case ${common.case_id} render parameters do not match the release profile`,
    );
  }
  if (!Array.isArray(render.runs) || render.runs.length !== 2) {
    throw new OfficeCorpusEvidenceError(
      `case ${common.case_id} must contain exactly two canonical render runs`,
    );
  }
  const runs = render.runs.map((item, index) => {
    const label = `case ${common.case_id}.render.runs[${index}]`;
    const run = record(item, label);
    exactKeys(
      run,
      ["run_id", "report_sha256", "canonical_pixel_sha256s"],
      label,
    );
    return {
      run_id: requiredString(run.run_id, `${label}.run_id`),
      report_sha256: digest(run.report_sha256, `${label}.report_sha256`),
      canonical_pixel_sha256s: validatePixelHashes(
        run.canonical_pixel_sha256s,
        `${label}.canonical_pixel_sha256s`,
      ),
    };
  });
  if (runs[0].run_id === runs[1].run_id) {
    throw new OfficeCorpusEvidenceError(
      `case ${common.case_id} canonical render run IDs must be distinct`,
    );
  }
  if (
    JSON.stringify(runs[0].canonical_pixel_sha256s) !==
    JSON.stringify(runs[1].canonical_pixel_sha256s)
  ) {
    throw new OfficeCorpusEvidenceError(
      `case ${common.case_id} canonical pixel hashes are not deterministic`,
    );
  }

  const structure = validateBooleanProof(
    value.structure,
    `case ${common.case_id}.structure`,
    ["artifact_sha256", "untouched_parts_match"],
  );
  if (structure.untouched_parts_match !== true) {
    throw new OfficeCorpusEvidenceError(
      `case ${common.case_id}.structure.untouched_parts_match must be true`,
    );
  }
  const normalizedStructure = {
    passed: true,
    report_sha256: digest(
      structure.report_sha256,
      `case ${common.case_id}.structure.report_sha256`,
    ),
    artifact_sha256: digest(
      structure.artifact_sha256,
      `case ${common.case_id}.structure.artifact_sha256`,
    ),
    untouched_parts_match: true,
  };
  if (normalizedStructure.artifact_sha256 !== finalSha256) {
    throw new OfficeCorpusEvidenceError(
      `case ${common.case_id} structure report is not bound to the final artifact`,
    );
  }

  let rewind = null;
  if (common.primary_bucket === "edit-rewind") {
    const proof = validateBooleanProof(value.rewind, `case ${common.case_id}.rewind`, [
      "checkpoint_sha256",
      "restored_sha256",
      "preview_cache_identity_sha256",
      "preview_cache_artifact_sha256",
      "ui_version_identity_sha256",
      "ui_version_artifact_sha256",
    ]);
    rewind = {
      passed: true,
      report_sha256: digest(
        proof.report_sha256,
        `case ${common.case_id}.rewind.report_sha256`,
      ),
      checkpoint_sha256: digest(
        proof.checkpoint_sha256,
        `case ${common.case_id}.rewind.checkpoint_sha256`,
      ),
      restored_sha256: digest(
        proof.restored_sha256,
        `case ${common.case_id}.rewind.restored_sha256`,
      ),
      preview_cache_identity_sha256: digest(
        proof.preview_cache_identity_sha256,
        `case ${common.case_id}.rewind.preview_cache_identity_sha256`,
      ),
      preview_cache_artifact_sha256: digest(
        proof.preview_cache_artifact_sha256,
        `case ${common.case_id}.rewind.preview_cache_artifact_sha256`,
      ),
      ui_version_identity_sha256: digest(
        proof.ui_version_identity_sha256,
        `case ${common.case_id}.rewind.ui_version_identity_sha256`,
      ),
      ui_version_artifact_sha256: digest(
        proof.ui_version_artifact_sha256,
        `case ${common.case_id}.rewind.ui_version_artifact_sha256`,
      ),
    };
    if (rewind.restored_sha256 !== common.source_sha256) {
      throw new OfficeCorpusEvidenceError(
        `case ${common.case_id} rewind did not restore the source digest`,
      );
    }
    if (
      rewind.preview_cache_artifact_sha256 !== common.source_sha256 ||
      rewind.ui_version_artifact_sha256 !== common.source_sha256
    ) {
      throw new OfficeCorpusEvidenceError(
        `case ${common.case_id} rewind cache and UI are not bound to the restored source`,
      );
    }
  } else if (value.rewind !== null) {
    throw new OfficeCorpusEvidenceError(
      `case ${common.case_id} must not claim rewind outside edit-rewind`,
    );
  }

  return {
    ...common,
    outcome: "supported",
    final_sha256: finalSha256,
    commit_count: commitCount,
    seal: normalizedSeal,
    independent_reopen: normalizedReopen,
    render: {
      artifact_sha256: renderArtifactSha256,
      parameters_manifest_sha256: renderParametersSha256,
      runs,
    },
    structure: normalizedStructure,
    rewind,
  };
}

function validateUnsupportedCase(value, common) {
  exactKeys(
    value,
    [
      "case_id",
      "format",
      "primary_bucket",
      "outcome",
      "source_sha256",
      "final_sha256",
      "commit_count",
      "rejection",
    ],
    `unsupported case ${common.case_id}`,
  );
  if (common.primary_bucket !== "unsupported") {
    throw new OfficeCorpusEvidenceError(
      `case ${common.case_id} unsupported outcome requires unsupported primary bucket`,
    );
  }
  if (value.commit_count !== 0) {
    throw new OfficeCorpusEvidenceError(
      `case ${common.case_id} unsupported commit_count must be zero`,
    );
  }
  const finalSha256 = digest(value.final_sha256, `case ${common.case_id}.final_sha256`);
  if (finalSha256 !== common.source_sha256) {
    throw new OfficeCorpusEvidenceError(
      `case ${common.case_id} unsupported source digest changed`,
    );
  }
  const rejection = validateBooleanProof(
    value.rejection,
    `case ${common.case_id}.rejection`,
    ["reason_code"],
  );
  return {
    ...common,
    outcome: "unsupported",
    final_sha256: finalSha256,
    commit_count: 0,
    rejection: {
      passed: true,
      report_sha256: digest(
        rejection.report_sha256,
        `case ${common.case_id}.rejection.report_sha256`,
      ),
      reason_code: requiredString(
        rejection.reason_code,
        `case ${common.case_id}.rejection.reason_code`,
      ),
    },
  };
}

function validateCase(value, index, environment) {
  const item = record(value, `cases[${index}]`);
  const caseId = requiredString(item.case_id, `cases[${index}].case_id`);
  const format = requiredString(item.format, `case ${caseId}.format`);
  if (!REQUIRED_OFFICE_CORPUS_FORMATS.includes(format)) {
    throw new OfficeCorpusEvidenceError(`case ${caseId} format is not supported`);
  }
  const primaryBucket = requiredString(
    item.primary_bucket,
    `case ${caseId}.primary_bucket`,
  );
  if (!REQUIRED_OFFICE_CORPUS_PRIMARY_BUCKETS.includes(primaryBucket)) {
    throw new OfficeCorpusEvidenceError(
      `case ${caseId} primary_bucket is not a v1.1 corpus bucket`,
    );
  }
  const outcome = requiredString(item.outcome, `case ${caseId}.outcome`);
  if (outcome !== "supported" && outcome !== "unsupported") {
    throw new OfficeCorpusEvidenceError(`case ${caseId} outcome is invalid`);
  }
  const common = {
    case_id: caseId,
    format,
    primary_bucket: primaryBucket,
    source_sha256: digest(item.source_sha256, `case ${caseId}.source_sha256`),
  };
  return outcome === "supported"
    ? validateSupportedCase(item, common, environment)
    : validateUnsupportedCase(item, common);
}

function coverageFor(cases) {
  const coverage = {};
  for (const format of REQUIRED_OFFICE_CORPUS_FORMATS) {
    const formatCases = cases.filter((item) => item.format === format);
    const buckets = Object.fromEntries(
      REQUIRED_OFFICE_CORPUS_PRIMARY_BUCKETS.map((bucket) => [
        bucket,
        formatCases.filter((item) => item.primary_bucket === bucket).length,
      ]),
    );
    coverage[format] = {
      total: formatCases.length,
      supported: formatCases.filter((item) => item.outcome === "supported").length,
      unsupported: formatCases.filter((item) => item.outcome === "unsupported").length,
      primary_buckets: buckets,
    };
  }
  return coverage;
}

function validateCoverage(cases) {
  if (!Array.isArray(cases) || cases.length !== OFFICE_CORPUS_CASE_COUNT) {
    throw new OfficeCorpusEvidenceError(
      `cases must contain exactly ${OFFICE_CORPUS_CASE_COUNT} entries`,
    );
  }
  const ids = cases.map((item) => item.case_id);
  for (let index = 1; index < ids.length; index += 1) {
    if (ids[index - 1] >= ids[index]) {
      throw new OfficeCorpusEvidenceError(
        "case IDs must be unique and sorted in ascending byte order",
      );
    }
  }
  const coverage = coverageFor(cases);
  for (const format of REQUIRED_OFFICE_CORPUS_FORMATS) {
    if (coverage[format].total < OFFICE_CORPUS_MINIMUM_PER_FORMAT) {
      throw new OfficeCorpusEvidenceError(
        `${format} must contain at least ${OFFICE_CORPUS_MINIMUM_PER_FORMAT} cases`,
      );
    }
    for (const bucket of REQUIRED_OFFICE_CORPUS_PRIMARY_BUCKETS) {
      if (
        coverage[format].primary_buckets[bucket] <
        OFFICE_CORPUS_MINIMUM_PER_PRIMARY_BUCKET
      ) {
        throw new OfficeCorpusEvidenceError(
          `${format}.${bucket} must contain at least ${OFFICE_CORPUS_MINIMUM_PER_PRIMARY_BUCKET} cases`,
        );
      }
    }
  }
  return coverage;
}

export function validateOfficeCorpusReport(
  value,
  {
    expectedCommit,
    expectedReleaseRef,
    expectedFrozenBackendIdentitySha256,
    expectedCorpusManifestSha256,
    expectedRendererIdentitySha256,
    expectedFontsManifestSha256,
    expectedParametersManifestSha256,
    expectedTarget,
  } = {},
) {
  const report = record(value, "Office corpus report");
  exactKeys(
    report,
    [
      "schema_version",
      "contract_version",
      "status",
      "run_id",
      "source_commit",
      "release_ref",
      "target",
      "started_at",
      "completed_at",
      "frozen_backend_identity_sha256",
      "frozen_backend_sha256",
      "corpus_manifest_sha256",
      "render_environment",
      "cases",
      "privacy",
    ],
    "Office corpus report",
  );
  if (report.schema_version !== OFFICE_CORPUS_SCHEMA_VERSION) {
    throw new OfficeCorpusEvidenceError("schema_version must be 1");
  }
  if (report.contract_version !== OFFICE_CORPUS_CONTRACT_VERSION) {
    throw new OfficeCorpusEvidenceError(
      `contract_version must be ${OFFICE_CORPUS_CONTRACT_VERSION}`,
    );
  }
  if (report.status !== "ok") {
    throw new OfficeCorpusEvidenceError("status must be ok");
  }
  const sourceCommit = commitId(report.source_commit, "source_commit");
  const candidateReleaseRef = releaseRef(report.release_ref, "release_ref");
  const frozenBackendIdentitySha256 = digest(
    report.frozen_backend_identity_sha256,
    "frozen_backend_identity_sha256",
  );
  const frozenBackendSha256 = digest(
    report.frozen_backend_sha256,
    "frozen_backend_sha256",
  );
  const corpusManifestSha256 = digest(
    report.corpus_manifest_sha256,
    "corpus_manifest_sha256",
  );
  const bindings = [
    [sourceCommit, expectedCommit, "source_commit does not match the release commit"],
    [candidateReleaseRef, expectedReleaseRef, "release_ref does not match the v1.1 tag"],
    [
      frozenBackendIdentitySha256,
      expectedFrozenBackendIdentitySha256,
      "frozen backend identity does not match the release matrix",
    ],
    [
      corpusManifestSha256,
      expectedCorpusManifestSha256,
      "corpus manifest digest does not match the release corpus",
    ],
  ];
  for (const [actual, expected, message] of bindings) {
    if (expected && actual !== expected) {
      throw new OfficeCorpusEvidenceError(message);
    }
  }
  const target = requiredString(report.target, "target", { maxLength: 32 });
  if (!REQUIRED_OFFICE_CORPUS_TARGETS.includes(target)) {
    throw new OfficeCorpusEvidenceError("target is not a v1.1 native target");
  }
  if (expectedTarget && target !== expectedTarget) {
    throw new OfficeCorpusEvidenceError("target does not match the expected native target");
  }
  const started = canonicalTimestamp(report.started_at, "started_at");
  const completed = canonicalTimestamp(report.completed_at, "completed_at");
  if (completed.milliseconds < started.milliseconds) {
    throw new OfficeCorpusEvidenceError("completed_at must not precede started_at");
  }
  const environment = validateRenderEnvironment(report.render_environment, {
    rendererIdentitySha256: expectedRendererIdentitySha256,
    fontsManifestSha256: expectedFontsManifestSha256,
    parametersManifestSha256: expectedParametersManifestSha256,
  });
  if (!Array.isArray(report.cases) || report.cases.length !== OFFICE_CORPUS_CASE_COUNT) {
    throw new OfficeCorpusEvidenceError(
      `cases must contain exactly ${OFFICE_CORPUS_CASE_COUNT} entries`,
    );
  }
  const cases = report.cases.map((item, index) =>
    validateCase(item, index, environment),
  );
  const coverage = validateCoverage(cases);
  return {
    schema_version: OFFICE_CORPUS_SCHEMA_VERSION,
    contract_version: OFFICE_CORPUS_CONTRACT_VERSION,
    status: "ok",
    run_id: requiredString(report.run_id, "run_id"),
    source_commit: sourceCommit,
    release_ref: candidateReleaseRef,
    target,
    started_at: started.text,
    completed_at: completed.text,
    frozen_backend_identity_sha256: frozenBackendIdentitySha256,
    frozen_backend_sha256: frozenBackendSha256,
    corpus_manifest_sha256: corpusManifestSha256,
    render_environment: environment,
    cases,
    coverage,
    privacy: validatePrivacy(report.privacy),
  };
}

function normalizeRawReport(item, index) {
  if (!(typeof item?.raw === "string" || Buffer.isBuffer(item?.raw))) {
    throw new OfficeCorpusEvidenceError(
      `report ${index + 1} must provide the original report bytes`,
    );
  }
  const bytes = Buffer.isBuffer(item.raw) ? item.raw : Buffer.from(item.raw, "utf8");
  if (bytes.length < 1 || bytes.length > MAX_REPORT_BYTES) {
    throw new OfficeCorpusEvidenceError(`report ${index + 1} has an invalid byte length`);
  }
  let raw;
  let value;
  try {
    raw = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
    value = JSON.parse(raw);
  } catch {
    throw new OfficeCorpusEvidenceError(`report ${index + 1} is not valid UTF-8 JSON`);
  }
  if (raw !== `${JSON.stringify(value)}\n`) {
    throw new OfficeCorpusEvidenceError(
      `report ${index + 1} must use canonical single-line JSON bytes`,
    );
  }
  return { bytes, value };
}

function descriptor(caseResult) {
  return [
    caseResult.case_id,
    caseResult.format,
    caseResult.primary_bucket,
    caseResult.outcome,
    caseResult.source_sha256,
  ];
}

function sameJson(left, right) {
  return JSON.stringify(left) === JSON.stringify(right);
}

export function aggregateOfficeCorpusEvidence(
  reports,
  {
    expectedCommit,
    expectedReleaseRef,
    expectedFrozenBackendIdentitySha256,
    expectedCorpusManifestSha256,
    expectedRendererIdentitySha256,
    expectedFontsManifestSha256,
    expectedParametersManifestSha256,
  } = {},
) {
  const expected = {
    expectedCommit: commitId(String(expectedCommit ?? "").toLowerCase(), "expectedCommit"),
    expectedReleaseRef: releaseRef(expectedReleaseRef, "expectedReleaseRef"),
    expectedFrozenBackendIdentitySha256: digest(
      expectedFrozenBackendIdentitySha256,
      "expectedFrozenBackendIdentitySha256",
    ),
    expectedCorpusManifestSha256: digest(
      expectedCorpusManifestSha256,
      "expectedCorpusManifestSha256",
    ),
    expectedRendererIdentitySha256: digest(
      expectedRendererIdentitySha256,
      "expectedRendererIdentitySha256",
    ),
    expectedFontsManifestSha256: digest(
      expectedFontsManifestSha256,
      "expectedFontsManifestSha256",
    ),
    expectedParametersManifestSha256: digest(
      expectedParametersManifestSha256,
      "expectedParametersManifestSha256",
    ),
  };
  if (!Array.isArray(reports) || reports.length !== REQUIRED_OFFICE_CORPUS_TARGETS.length) {
    throw new OfficeCorpusEvidenceError(
      `exactly ${REQUIRED_OFFICE_CORPUS_TARGETS.length} native target reports are required`,
    );
  }
  const normalized = reports.map((item, index) => {
    const raw = normalizeRawReport(item, index);
    const report = validateOfficeCorpusReport(raw.value, expected);
    return { report, report_sha256: sha256(raw.bytes) };
  });

  const targetSet = new Set(normalized.map(({ report }) => report.target));
  const runIdSet = new Set(normalized.map(({ report }) => report.run_id));
  if (
    targetSet.size !== REQUIRED_OFFICE_CORPUS_TARGETS.length ||
    REQUIRED_OFFICE_CORPUS_TARGETS.some((target) => !targetSet.has(target))
  ) {
    throw new OfficeCorpusEvidenceError(
      "reports must contain exactly one of each v1.1 native target",
    );
  }
  if (runIdSet.size !== REQUIRED_OFFICE_CORPUS_TARGETS.length) {
    throw new OfficeCorpusEvidenceError("native target run_id values must be distinct");
  }

  const baseline = normalized[0].report;
  const baselineDescriptors = baseline.cases.map(descriptor);
  const baselineLogicalIdentity = {
    renderer_id: baseline.render_environment.renderer.id,
    renderer_identity_manifest_sha256:
      baseline.render_environment.renderer.identity_manifest_sha256,
    frozen_backend_identity_sha256: baseline.frozen_backend_identity_sha256,
    fonts: baseline.render_environment.fonts,
    parameters: baseline.render_environment.parameters,
  };
  for (const { report } of normalized.slice(1)) {
    if (!sameJson(report.cases.map(descriptor), baselineDescriptors)) {
      throw new OfficeCorpusEvidenceError(
        "all six native targets must execute identical case IDs and corpus descriptors",
      );
    }
    const logicalIdentity = {
      renderer_id: report.render_environment.renderer.id,
      renderer_identity_manifest_sha256:
        report.render_environment.renderer.identity_manifest_sha256,
      frozen_backend_identity_sha256: report.frozen_backend_identity_sha256,
      fonts: report.render_environment.fonts,
      parameters: report.render_environment.parameters,
    };
    if (!sameJson(logicalIdentity, baselineLogicalIdentity)) {
      throw new OfficeCorpusEvidenceError(
        "all six native targets must use one backend, renderer, font, and parameter identity",
      );
    }
  }

  normalized.sort(
    (left, right) =>
      REQUIRED_OFFICE_CORPUS_TARGETS.indexOf(left.report.target) -
      REQUIRED_OFFICE_CORPUS_TARGETS.indexOf(right.report.target),
  );
  const caseIds = baseline.cases.map((item) => item.case_id);
  const privacy = {
    document_content_collected: false,
    file_paths_collected: false,
    rendered_bytes_collected: false,
  };
  return {
    schema_version: OFFICE_CORPUS_SCHEMA_VERSION,
    contract_version: OFFICE_CORPUS_CONTRACT_VERSION,
    status: "ok",
    source_commit: expected.expectedCommit,
    release_ref: expected.expectedReleaseRef,
    frozen_backend_identity_sha256:
      expected.expectedFrozenBackendIdentitySha256,
    corpus_manifest_sha256: expected.expectedCorpusManifestSha256,
    renderer_identity_sha256: expected.expectedRendererIdentitySha256,
    fonts_manifest_sha256: expected.expectedFontsManifestSha256,
    parameters_manifest_sha256: expected.expectedParametersManifestSha256,
    target_count: REQUIRED_OFFICE_CORPUS_TARGETS.length,
    case_count: OFFICE_CORPUS_CASE_COUNT,
    case_ids_sha256: sha256(Buffer.from(`${JSON.stringify(caseIds)}\n`, "utf8")),
    coverage: baseline.coverage,
    targets: normalized.map(({ report, report_sha256: reportSha256 }) => ({
      target: report.target,
      run_id: report.run_id,
      report_sha256: reportSha256,
      started_at: report.started_at,
      completed_at: report.completed_at,
      frozen_backend_sha256: report.frozen_backend_sha256,
      renderer: {
        id: report.render_environment.renderer.id,
        version: report.render_environment.renderer.version,
        executable_sha256: report.render_environment.renderer.executable_sha256,
        bundle_tree_sha256: report.render_environment.renderer.bundle_tree_sha256,
        attestation_sha256: report.render_environment.renderer.attestation_sha256,
      },
      supported_case_count: report.cases.filter((item) => item.outcome === "supported")
        .length,
      unsupported_case_count: report.cases.filter(
        (item) => item.outcome === "unsupported",
      ).length,
    })),
    privacy,
  };
}

function readStableReport(pathname) {
  const before = lstatSync(pathname);
  if (
    !before.isFile() ||
    before.isSymbolicLink() ||
    before.size < 1 ||
    before.size > MAX_REPORT_BYTES
  ) {
    throw new OfficeCorpusEvidenceError("corpus report must be a bounded regular file");
  }
  const bytes = readFileSync(pathname);
  const after = lstatSync(pathname);
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
    throw new OfficeCorpusEvidenceError("corpus report changed while it was read");
  }
  return { raw: bytes };
}

function findReports(root) {
  const rootPath = resolve(root);
  const rootInfo = lstatSync(rootPath);
  if (!rootInfo.isDirectory() || rootInfo.isSymbolicLink()) {
    throw new OfficeCorpusEvidenceError("corpus reports root must be a regular directory");
  }
  const reports = [];
  const visit = (directory) => {
    for (const entry of readdirSync(directory, { withFileTypes: true })) {
      const pathname = join(directory, entry.name);
      if (entry.isSymbolicLink()) {
        throw new OfficeCorpusEvidenceError(
          "corpus evidence tree must not contain symbolic links",
        );
      }
      if (entry.isDirectory()) {
        visit(pathname);
      } else if (entry.isFile() && entry.name === OFFICE_CORPUS_REPORT_FILENAME) {
        reports.push(readStableReport(pathname));
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
    throw new OfficeCorpusEvidenceError("Office corpus summary output already exists");
  } catch (error) {
    if (error instanceof OfficeCorpusEvidenceError) throw error;
    if (error?.code !== "ENOENT") throw error;
  }
  const temporary = `${destination}.tmp-${process.pid}`;
  try {
    writeFileSync(temporary, `${JSON.stringify(value, null, 2)}\n`, {
      encoding: "utf8",
      mode: 0o600,
      flag: "wx",
    });
    // A POSIX rename would replace a destination created after the lstat
    // check. Linking the completed same-directory temporary file is the
    // cross-process no-overwrite publication primitive; it fails with EEXIST.
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
  if (argv.length !== 10 || argv[0] !== "aggregate") {
    throw new OfficeCorpusEvidenceError(
      "usage: v11-office-corpus-evidence.mjs aggregate <reports-dir> <commit> " +
        "<v1.1-release-ref> <frozen-backend-identity-sha256> <corpus-manifest-sha256> " +
        "<renderer-identity-sha256> <fonts-manifest-sha256> " +
        "<parameters-manifest-sha256> <output>",
    );
  }
  const [
    ,
    reportsDirectory,
    expectedCommit,
    expectedReleaseRef,
    expectedFrozenBackendIdentitySha256,
    expectedCorpusManifestSha256,
    expectedRendererIdentitySha256,
    expectedFontsManifestSha256,
    expectedParametersManifestSha256,
    output,
  ] = argv;
  const summary = aggregateOfficeCorpusEvidence(findReports(reportsDirectory), {
    expectedCommit: String(expectedCommit).toLowerCase(),
    expectedReleaseRef,
    expectedFrozenBackendIdentitySha256,
    expectedCorpusManifestSha256,
    expectedRendererIdentitySha256,
    expectedFontsManifestSha256,
    expectedParametersManifestSha256,
  });
  writeNewOutput(output, summary);
  process.stdout.write(
    `[v11-office-corpus-evidence] verified ${summary.case_count} cases on ${summary.target_count} native targets\n`,
  );
}

if (resolve(process.argv[1] ?? "") === fileURLToPath(import.meta.url)) {
  try {
    main(process.argv.slice(2));
  } catch (error) {
    process.stderr.write(`[v11-office-corpus-evidence] ${error.message}\n`);
    process.exitCode = 1;
  }
}
