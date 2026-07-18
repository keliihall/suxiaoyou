import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { spawnSync } from "node:child_process";
import {
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  symlinkSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import {
  OFFICE_CORPUS_CASE_COUNT,
  OFFICE_CORPUS_CONTRACT_VERSION,
  OFFICE_CORPUS_REPORT_FILENAME,
  REQUIRED_OFFICE_CORPUS_FORMATS,
  REQUIRED_OFFICE_CORPUS_PRIMARY_BUCKETS,
  REQUIRED_OFFICE_CORPUS_TARGETS,
  OfficeCorpusEvidenceError,
  aggregateOfficeCorpusEvidence,
  validateOfficeCorpusReport,
} from "./v11-office-corpus-evidence.mjs";

const SCRIPT = fileURLToPath(
  new URL("./v11-office-corpus-evidence.mjs", import.meta.url),
);
const COMMIT = "a".repeat(40);
const RELEASE_REF = "v1.1.0-rc.4";
const FROZEN_BACKEND_IDENTITY_SHA256 = digest("final-frozen-backend-matrix");
const CORPUS_MANIFEST_SHA256 = digest("fixed-corpus-manifest");
const RENDERER_IDENTITY_SHA256 = digest("authoritative-renderer-matrix");
const FONTS_MANIFEST_SHA256 = digest("frozen-cjk-font-set");
const PARAMETERS_MANIFEST_SHA256 = digest("canonical-render-parameters");

function digest(value) {
  return createHash("sha256").update(value).digest("hex");
}

function expectedBindings() {
  return {
    expectedCommit: COMMIT,
    expectedReleaseRef: RELEASE_REF,
    expectedFrozenBackendIdentitySha256: FROZEN_BACKEND_IDENTITY_SHA256,
    expectedCorpusManifestSha256: CORPUS_MANIFEST_SHA256,
    expectedRendererIdentitySha256: RENDERER_IDENTITY_SHA256,
    expectedFontsManifestSha256: FONTS_MANIFEST_SHA256,
    expectedParametersManifestSha256: PARAMETERS_MANIFEST_SHA256,
  };
}

function supportedCase(caseId, format, bucket, target) {
  const sourceSha256 = digest(`source:${caseId}`);
  const finalSha256 = digest(`final:${target}:${caseId}`);
  const pixels = [
    digest(`pixel:${target}:${caseId}:1`),
    digest(`pixel:${target}:${caseId}:2`),
  ];
  return {
    case_id: caseId,
    format,
    primary_bucket: bucket,
    outcome: "supported",
    source_sha256: sourceSha256,
    final_sha256: finalSha256,
    commit_count: 1,
    seal: {
      passed: true,
      report_sha256: digest(`seal-report:${target}:${caseId}`),
      artifact_sha256: finalSha256,
    },
    independent_reopen: {
      passed: true,
      report_sha256: digest(`reopen-report:${target}:${caseId}`),
      artifact_sha256: finalSha256,
    },
    render: {
      artifact_sha256: finalSha256,
      parameters_manifest_sha256: PARAMETERS_MANIFEST_SHA256,
      runs: [
        {
          run_id: `render-a:${caseId}`,
          report_sha256: digest(`render-a-report:${target}:${caseId}`),
          canonical_pixel_sha256s: pixels,
        },
        {
          run_id: `render-b:${caseId}`,
          report_sha256: digest(`render-b-report:${target}:${caseId}`),
          canonical_pixel_sha256s: [...pixels],
        },
      ],
    },
    structure: {
      passed: true,
      report_sha256: digest(`structure-report:${target}:${caseId}`),
      artifact_sha256: finalSha256,
      untouched_parts_match: true,
    },
    rewind:
      bucket === "edit-rewind"
        ? {
            passed: true,
            report_sha256: digest(`rewind-report:${target}:${caseId}`),
            checkpoint_sha256: digest(`checkpoint:${target}:${caseId}`),
            restored_sha256: sourceSha256,
            preview_cache_identity_sha256: digest(
              `cache:${target}:${caseId}`,
            ),
            preview_cache_artifact_sha256: sourceSha256,
            ui_version_identity_sha256: digest(`ui:${target}:${caseId}`),
            ui_version_artifact_sha256: sourceSha256,
          }
        : null,
  };
}

function unsupportedCase(caseId, format) {
  const sourceSha256 = digest(`source:${caseId}`);
  return {
    case_id: caseId,
    format,
    primary_bucket: "unsupported",
    outcome: "unsupported",
    source_sha256: sourceSha256,
    final_sha256: sourceSha256,
    commit_count: 0,
    rejection: {
      passed: true,
      report_sha256: digest(`rejection-report:${caseId}`),
      reason_code: "unsupported-input",
    },
  };
}

function corpusCases(target) {
  const cases = [];
  for (const format of REQUIRED_OFFICE_CORPUS_FORMATS) {
    for (const bucket of REQUIRED_OFFICE_CORPUS_PRIMARY_BUCKETS) {
      for (let index = 0; index < 25; index += 1) {
        const caseId = `${format}-${bucket}-${String(index).padStart(3, "0")}`;
        cases.push(
          bucket === "unsupported"
            ? unsupportedCase(caseId, format)
            : supportedCase(caseId, format, bucket, target),
        );
      }
    }
  }
  return cases.sort((left, right) =>
    left.case_id < right.case_id ? -1 : left.case_id > right.case_id ? 1 : 0,
  );
}

function passingReport(target, index = REQUIRED_OFFICE_CORPUS_TARGETS.indexOf(target)) {
  const attestationSha256 = digest(`renderer-attestation:${target}`);
  return {
    schema_version: 1,
    contract_version: OFFICE_CORPUS_CONTRACT_VERSION,
    status: "ok",
    run_id: `corpus-run:${target}`,
    source_commit: COMMIT,
    release_ref: RELEASE_REF,
    target,
    started_at: `2026-07-18T0${index}:00:00.000Z`,
    completed_at: `2026-07-18T0${index}:30:00.000Z`,
    frozen_backend_identity_sha256: FROZEN_BACKEND_IDENTITY_SHA256,
    frozen_backend_sha256: digest(`final-frozen-backend:${target}`),
    corpus_manifest_sha256: CORPUS_MANIFEST_SHA256,
    render_environment: {
      renderer: {
        authoritative: true,
        id: "suxiaoyou-attested-office",
        version: `attestation-${attestationSha256}`,
        identity_manifest_sha256: RENDERER_IDENTITY_SHA256,
        executable_sha256: digest(`renderer-executable:${target}`),
        bundle_tree_sha256: digest(`renderer-bundle:${target}`),
        attestation_sha256: attestationSha256,
      },
      fonts: {
        set_id: "suxiaoyou-cjk-2026-07",
        manifest_sha256: FONTS_MANIFEST_SHA256,
      },
      parameters: {
        profile_id: "office-canonical-v1",
        manifest_sha256: PARAMETERS_MANIFEST_SHA256,
      },
    },
    cases: corpusCases(target),
    privacy: {
      document_content_collected: false,
      file_paths_collected: false,
      rendered_bytes_collected: false,
    },
  };
}

function canonicalRaw(value) {
  return `${JSON.stringify(value)}\n`;
}

function reportInputs(values = REQUIRED_OFFICE_CORPUS_TARGETS.map(passingReport)) {
  return values.map((value) => ({ raw: canonicalRaw(value) }));
}

test("accepts exactly the same 300 release-bound cases on all five native targets", () => {
  const summary = aggregateOfficeCorpusEvidence(reportInputs(), expectedBindings());
  assert.equal(summary.status, "ok");
  assert.equal(summary.target_count, 5);
  assert.equal(summary.case_count, OFFICE_CORPUS_CASE_COUNT);
  assert.deepEqual(
    summary.targets.map((item) => item.target),
    REQUIRED_OFFICE_CORPUS_TARGETS,
  );
  for (const format of REQUIRED_OFFICE_CORPUS_FORMATS) {
    assert.equal(summary.coverage[format].total, 100);
    for (const bucket of REQUIRED_OFFICE_CORPUS_PRIMARY_BUCKETS) {
      assert.equal(summary.coverage[format].primary_buckets[bucket], 25);
    }
  }
  assert.equal(
    summary.frozen_backend_identity_sha256,
    FROZEN_BACKEND_IDENTITY_SHA256,
  );
  assert.equal(summary.corpus_manifest_sha256, CORPUS_MANIFEST_SHA256);
  assert.equal(summary.renderer_identity_sha256, RENDERER_IDENTITY_SHA256);
  assert.equal(summary.fonts_manifest_sha256, FONTS_MANIFEST_SHA256);
  assert.equal(summary.parameters_manifest_sha256, PARAMETERS_MANIFEST_SHA256);
});

test("publishes the digest of each exact raw report without paths or document content", () => {
  const reports = reportInputs();
  reports[0].path = "/private/corpus/customer-secret.docx";
  const summary = aggregateOfficeCorpusEvidence(reports, expectedBindings());
  assert.equal(
    summary.targets[0].report_sha256,
    createHash("sha256").update(reports[0].raw).digest("hex"),
  );
  const serialized = JSON.stringify(summary);
  assert.doesNotMatch(serialized, /private|customer-secret|\.docx/u);
  assert.deepEqual(summary.privacy, {
    document_content_collected: false,
    file_paths_collected: false,
    rendered_bytes_collected: false,
  });
});

test("requires original canonical bytes and rejects duplicate-key or convenience-value ambiguity", () => {
  const report = passingReport(REQUIRED_OFFICE_CORPUS_TARGETS[0]);
  assert.throws(
    () =>
      aggregateOfficeCorpusEvidence(
        [
          { value: report },
          ...reportInputs().slice(1),
        ],
        expectedBindings(),
      ),
    /original report bytes/u,
  );

  const pretty = `${JSON.stringify(report, null, 2)}\n`;
  assert.throws(
    () =>
      aggregateOfficeCorpusEvidence(
        [{ raw: pretty }, ...reportInputs().slice(1)],
        expectedBindings(),
      ),
    /canonical single-line JSON bytes/u,
  );

  const duplicateKey = canonicalRaw(report).replace(
    '"schema_version":1,',
    '"schema_version":1,"schema_version":1,',
  );
  assert.throws(
    () =>
      aggregateOfficeCorpusEvidence(
        [{ raw: duplicateKey }, ...reportInputs().slice(1)],
        expectedBindings(),
      ),
    /canonical single-line JSON bytes/u,
  );
});

test("binds tag, commit, final frozen backend, corpus, renderer, fonts, and parameters", () => {
  const fields = [
    ["source_commit", "b".repeat(40), /release commit/u],
    ["release_ref", "v1.1.0-rc.9", /v1\.1 tag/u],
    [
      "frozen_backend_identity_sha256",
      digest("other-backend-matrix"),
      /release matrix/u,
    ],
    ["corpus_manifest_sha256", digest("other-corpus"), /release corpus/u],
  ];
  for (const [field, value, pattern] of fields) {
    const report = passingReport(REQUIRED_OFFICE_CORPUS_TARGETS[0]);
    report[field] = value;
    assert.throws(
      () => validateOfficeCorpusReport(report, expectedBindings()),
      pattern,
    );
  }

  const identityFields = [
    ["renderer", "identity_manifest_sha256", /renderer identity/u],
    ["fonts", "manifest_sha256", /font manifest/u],
    ["parameters", "manifest_sha256", /render parameters/u],
  ];
  for (const [section, field, pattern] of identityFields) {
    const report = passingReport(REQUIRED_OFFICE_CORPUS_TARGETS[0]);
    report.render_environment[section][field] = digest(`other:${section}`);
    assert.throws(
      () => validateOfficeCorpusReport(report, expectedBindings()),
      pattern,
    );
  }
});

test("rejects missing targets, duplicate targets, and corpus descriptor drift", () => {
  assert.throws(
    () =>
      aggregateOfficeCorpusEvidence(reportInputs().slice(0, 4), expectedBindings()),
    /exactly 5/u,
  );

  const duplicateTargets = REQUIRED_OFFICE_CORPUS_TARGETS.map(passingReport);
  duplicateTargets[4].target = duplicateTargets[3].target;
  duplicateTargets[4].run_id = "duplicate-target-run";
  assert.throws(
    () => aggregateOfficeCorpusEvidence(reportInputs(duplicateTargets), expectedBindings()),
    /exactly one of each/u,
  );

  const drift = REQUIRED_OFFICE_CORPUS_TARGETS.map(passingReport);
  drift[4].cases[0].source_sha256 = digest("different-corpus-source");
  assert.throws(
    () => aggregateOfficeCorpusEvidence(reportInputs(drift), expectedBindings()),
    /identical case IDs and corpus descriptors/u,
  );

  const logicalRendererDrift = REQUIRED_OFFICE_CORPUS_TARGETS.map(passingReport);
  logicalRendererDrift[4].render_environment.renderer.id = "another-renderer";
  assert.throws(
    () =>
      aggregateOfficeCorpusEvidence(
        reportInputs(logicalRendererDrift),
        expectedBindings(),
      ),
    /attested runtime identity/u,
  );

  const logicalFontDrift = REQUIRED_OFFICE_CORPUS_TARGETS.map(passingReport);
  logicalFontDrift[4].render_environment.fonts.set_id = "another-font-set";
  assert.throws(
    () =>
      aggregateOfficeCorpusEvidence(
        reportInputs(logicalFontDrift),
        expectedBindings(),
      ),
    /one backend, renderer, font, and parameter identity/u,
  );
});

test("enforces 300 unique sorted IDs, 100 per format, and 20 per primary bucket", () => {
  const short = passingReport(REQUIRED_OFFICE_CORPUS_TARGETS[0]);
  short.cases.pop();
  assert.throws(
    () => validateOfficeCorpusReport(short, expectedBindings()),
    /exactly 300/u,
  );

  const unsorted = passingReport(REQUIRED_OFFICE_CORPUS_TARGETS[0]);
  [unsorted.cases[0], unsorted.cases[1]] = [unsorted.cases[1], unsorted.cases[0]];
  assert.throws(
    () => validateOfficeCorpusReport(unsorted, expectedBindings()),
    /unique and sorted/u,
  );

  const tooFewDocx = passingReport(REQUIRED_OFFICE_CORPUS_TARGETS[0]);
  tooFewDocx.cases[0].format = "xlsx";
  assert.throws(
    () => validateOfficeCorpusReport(tooFewDocx, expectedBindings()),
    /docx must contain at least 100/u,
  );

  const tooFewCjk = passingReport(REQUIRED_OFFICE_CORPUS_TARGETS[0]);
  const docxCjk = tooFewCjk.cases.filter(
    (item) => item.format === "docx" && item.primary_bucket === "cjk",
  );
  for (const item of docxCjk.slice(0, 6)) item.primary_bucket = "complex";
  assert.throws(
    () => validateOfficeCorpusReport(tooFewCjk, expectedBindings()),
    /docx\.cjk must contain at least 20/u,
  );
});

test("supported cases require commit seal, independent reopen, structure, and two deterministic renders", () => {
  const mutations = [
    [
      (item) => {
        item.commit_count = 0;
      },
      /positive safe integer/u,
    ],
    [
      (item) => {
        item.seal.passed = false;
      },
      /seal\.passed must be true/u,
    ],
    [
      (item) => {
        item.independent_reopen.artifact_sha256 = digest("wrong-artifact");
      },
      /independent reopen is not bound/u,
    ],
    [
      (item) => {
        item.render.runs.pop();
      },
      /exactly two canonical render runs/u,
    ],
    [
      (item) => {
        item.render.runs[1].canonical_pixel_sha256s[0] =
          digest("different-pixel");
      },
      /pixel hashes are not deterministic/u,
    ],
    [
      (item) => {
        item.structure.untouched_parts_match = false;
      },
      /untouched_parts_match must be true/u,
    ],
  ];
  for (const [mutate, pattern] of mutations) {
    const report = passingReport(REQUIRED_OFFICE_CORPUS_TARGETS[0]);
    mutate(report.cases.find((item) => item.primary_bucket === "cjk"));
    assert.throws(
      () => validateOfficeCorpusReport(report, expectedBindings()),
      pattern,
    );
  }
});

test("edit-rewind cases require a source-restoring rewind bound to cache and UI identities", () => {
  const missing = passingReport(REQUIRED_OFFICE_CORPUS_TARGETS[0]);
  missing.cases.find((item) => item.primary_bucket === "edit-rewind").rewind = null;
  assert.throws(
    () => validateOfficeCorpusReport(missing, expectedBindings()),
    /rewind must be an object/u,
  );

  const staleSurfaces = passingReport(REQUIRED_OFFICE_CORPUS_TARGETS[0]);
  staleSurfaces.cases.find(
    (item) => item.primary_bucket === "edit-rewind",
  ).rewind.preview_cache_artifact_sha256 = digest("stale-preview");
  assert.throws(
    () => validateOfficeCorpusReport(staleSurfaces, expectedBindings()),
    /cache and UI are not bound/u,
  );

  const wrongDigest = passingReport(REQUIRED_OFFICE_CORPUS_TARGETS[0]);
  wrongDigest.cases.find(
    (item) => item.primary_bucket === "edit-rewind",
  ).rewind.restored_sha256 = digest("not-the-source");
  assert.throws(
    () => validateOfficeCorpusReport(wrongDigest, expectedBindings()),
    /did not restore the source digest/u,
  );

  const falseClaim = passingReport(REQUIRED_OFFICE_CORPUS_TARGETS[0]);
  falseClaim.cases.find((item) => item.primary_bucket === "cjk").rewind =
    structuredClone(
      falseClaim.cases.find((item) => item.primary_bucket === "edit-rewind").rewind,
    );
  assert.throws(
    () => validateOfficeCorpusReport(falseClaim, expectedBindings()),
    /must not claim rewind outside edit-rewind/u,
  );
});

test("unsupported cases prove zero commits and an unchanged source digest", () => {
  const committed = passingReport(REQUIRED_OFFICE_CORPUS_TARGETS[0]);
  committed.cases.find((item) => item.outcome === "unsupported").commit_count = 1;
  assert.throws(
    () => validateOfficeCorpusReport(committed, expectedBindings()),
    /commit_count must be zero/u,
  );

  const changed = passingReport(REQUIRED_OFFICE_CORPUS_TARGETS[0]);
  changed.cases.find((item) => item.outcome === "unsupported").final_sha256 =
    digest("mutated-source");
  assert.throws(
    () => validateOfficeCorpusReport(changed, expectedBindings()),
    /source digest changed/u,
  );
});

test("supported cases cannot satisfy the corpus with a no-op commit", () => {
  const noOp = passingReport(REQUIRED_OFFICE_CORPUS_TARGETS[0]);
  const item = noOp.cases.find((candidate) => candidate.outcome === "supported");
  item.final_sha256 = item.source_sha256;

  assert.throws(
    () => validateOfficeCorpusReport(noOp, expectedBindings()),
    /must differ from its source/u,
  );
});

test("strict schemas reject paths, content fields, multi-bucket arrays, and placeholders", () => {
  const extraContent = passingReport(REQUIRED_OFFICE_CORPUS_TARGETS[0]);
  extraContent.cases[0].document_content = "private text";
  assert.throws(
    () => validateOfficeCorpusReport(extraContent, expectedBindings()),
    /fields must be exactly/u,
  );

  const pathLikeId = passingReport(REQUIRED_OFFICE_CORPUS_TARGETS[0]);
  pathLikeId.cases[0].case_id = "customer/secret.docx";
  assert.throws(
    () => validateOfficeCorpusReport(pathLikeId, expectedBindings()),
    /case_id is invalid/u,
  );

  const bucketArray = passingReport(REQUIRED_OFFICE_CORPUS_TARGETS[0]);
  bucketArray.cases[0].primary_bucket = ["cjk", "complex"];
  assert.throws(
    () => validateOfficeCorpusReport(bucketArray, expectedBindings()),
    /primary_bucket is invalid/u,
  );

  const placeholder = passingReport(REQUIRED_OFFICE_CORPUS_TARGETS[0]);
  placeholder.frozen_backend_sha256 = "0".repeat(64);
  assert.throws(
    () => validateOfficeCorpusReport(placeholder, expectedBindings()),
    /frozen_backend_sha256 is invalid/u,
  );
});

test("CLI discovers five regular reports and writes a new machine summary", (t) => {
  const root = mkdtempSync(join(tmpdir(), "suyo-v11-office-corpus-"));
  t.after(() => rmSync(root, { recursive: true, force: true }));
  for (const [index, target] of REQUIRED_OFFICE_CORPUS_TARGETS.entries()) {
    const directory = join(root, `runner-${index}`);
    mkdirSync(directory);
    writeFileSync(
      join(directory, OFFICE_CORPUS_REPORT_FILENAME),
      canonicalRaw(passingReport(target, index)),
    );
  }
  const output = join(root, "summary", "V11-OFFICE-CORPUS-EVIDENCE.json");
  const args = [
    SCRIPT,
    "aggregate",
    root,
    COMMIT,
    RELEASE_REF,
    FROZEN_BACKEND_IDENTITY_SHA256,
    CORPUS_MANIFEST_SHA256,
    RENDERER_IDENTITY_SHA256,
    FONTS_MANIFEST_SHA256,
    PARAMETERS_MANIFEST_SHA256,
    output,
  ];
  const result = spawnSync(process.execPath, args, { encoding: "utf8" });
  assert.equal(result.status, 0, result.stderr);
  assert.match(result.stdout, /verified 300 cases on 5 native targets/u);
  const summary = JSON.parse(readFileSync(output, "utf8"));
  assert.equal(summary.case_count, 300);
  assert.equal(summary.target_count, 5);

  const overwrite = spawnSync(process.execPath, args, { encoding: "utf8" });
  assert.notEqual(overwrite.status, 0);
  assert.match(overwrite.stderr, /already exists/u);
});

test("CLI rejects every symlink in the evidence tree and has no report generator mode", (t) => {
  const root = mkdtempSync(join(tmpdir(), "suyo-v11-office-corpus-link-"));
  t.after(() => rmSync(root, { recursive: true, force: true }));
  const real = join(root, "real.json");
  writeFileSync(real, canonicalRaw(passingReport(REQUIRED_OFFICE_CORPUS_TARGETS[0])));
  symlinkSync(real, join(root, OFFICE_CORPUS_REPORT_FILENAME));
  const output = join(root, "out.json");
  const result = spawnSync(
    process.execPath,
    [
      SCRIPT,
      "aggregate",
      root,
      COMMIT,
      RELEASE_REF,
      FROZEN_BACKEND_IDENTITY_SHA256,
      CORPUS_MANIFEST_SHA256,
      RENDERER_IDENTITY_SHA256,
      FONTS_MANIFEST_SHA256,
      PARAMETERS_MANIFEST_SHA256,
      output,
    ],
    { encoding: "utf8" },
  );
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /must not contain symbolic links/u);

  const generate = spawnSync(process.execPath, [SCRIPT, "generate"], {
    encoding: "utf8",
  });
  assert.notEqual(generate.status, 0);
  assert.match(generate.stderr, /usage:/u);
  assert.equal(readFileSync(real, "utf8").includes("customer"), false);
});

test("public validation errors do not echo private values", () => {
  assert.throws(
    () => validateOfficeCorpusReport({ secret: "do-not-leak" }, expectedBindings()),
    OfficeCorpusEvidenceError,
  );
  try {
    validateOfficeCorpusReport({ secret: "do-not-leak" }, expectedBindings());
  } catch (error) {
    assert.doesNotMatch(error.message, /do-not-leak/u);
  }
});
