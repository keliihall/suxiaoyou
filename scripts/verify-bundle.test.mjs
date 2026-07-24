import assert from "node:assert/strict";
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { spawnSync } from "node:child_process";
import test from "node:test";

const verifyBundleScript = fileURLToPath(
  new URL("./verify-bundle.mjs", import.meta.url),
);
const officeRepairPromptSource = fileURLToPath(
  new URL("../backend/app/agent/prompts/office_repair.txt", import.meta.url),
);
const executableName =
  process.platform === "win32" ? "suxiaoyou-backend.exe" : "suxiaoyou-backend";

// This intentionally mirrors every portable static requirement enforced by
// verify-bundle.mjs. Adding or removing a shipping requirement must update this
// fixture contract explicitly, so a platform-specific filesystem layout cannot
// silently weaken the release gate.
const REQUIRED_STATIC_ASSETS = [
  { kind: "file", relativePath: executableName },
  { kind: "dir", relativePath: "_internal" },
  { kind: "nonempty-dir", relativePath: "_internal/alembic" },
  { kind: "file", relativePath: "_internal/alembic.ini" },
  {
    kind: "file",
    relativePath:
      "_internal/alembic/versions/0010_v110_checkpoint_ledger.py",
  },
  {
    kind: "file",
    relativePath:
      "_internal/alembic/versions/0011_v110_user_office_templates.py",
  },
  {
    kind: "file",
    relativePath:
      "_internal/alembic/versions/0012_v110_workspace_identity_v2.py",
  },
  { kind: "nonempty-dir", relativePath: "_internal/app/agent/prompts" },
  {
    kind: "file",
    relativePath: "_internal/app/agent/prompts/validator.txt",
  },
  {
    kind: "file",
    relativePath: "_internal/app/agent/prompts/office_repair.txt",
  },
  { kind: "file", relativePath: "_internal/app/data/connectors.json" },
  { kind: "dir", relativePath: "_internal/app/data/skills" },
  { kind: "dir", relativePath: "_internal/app/data/plugins" },
  {
    kind: "file",
    relativePath: "_internal/app/data/fonts/SuxiaoyouCJK-Regular.ttf",
  },
  {
    kind: "file",
    relativePath: "_internal/app/data/fonts/OFL-1.1.txt",
  },
  {
    kind: "file",
    relativePath: "_internal/app/data/fonts/PROVENANCE.md",
  },
  {
    kind: "file",
    relativePath: "_internal/app/office_templates/assets/catalog.json",
  },
  {
    kind: "file",
    relativePath: "_internal/app/office_templates/assets/catalog.sig.json",
  },
  {
    kind: "file",
    relativePath:
      "_internal/app/office_templates/assets/templates/business-brief.docx",
  },
  {
    kind: "file",
    relativePath:
      "_internal/app/office_templates/assets/templates/project-tracker.xlsx",
  },
  {
    kind: "file",
    relativePath:
      "_internal/app/office_templates/assets/templates/status-update.pptx",
  },
  { kind: "dir", relativePath: "_internal/frontend_out" },
  { kind: "file", relativePath: "_internal/frontend_out/m.html" },
  { kind: "file", relativePath: "_internal/frontend_out/index.html" },
  {
    kind: "nonempty-dir",
    relativePath: "_internal/frontend_out/_next/static",
  },
  { kind: "nonempty-dir", relativePath: "_internal/uvicorn" },
  { kind: "nonempty-dir", relativePath: "_internal/pydantic_core" },
  { kind: "nonempty-dir", relativePath: "_internal/app" },
];

test("accepts a complete bundle when pure-Python SQLAlchemy only lives in PYZ", (t) => {
  const dist = createCompleteStaticBundle(t);
  const result = verifyBundle(dist);

  assert.equal(result.status, 0, result.stderr);
  assert.match(result.stdout, /static: 28 required assets present/);
  assert.doesNotMatch(result.stderr, /sqlalchemy/i);
});

test("runtime smoke covers released ACP and both Office renderer profiles", () => {
  const source = readFileSync(verifyBundleScript, "utf8");

  assert.match(source, /verifyAcpClosedGate/);
  assert.match(source, /expectedV11GateMode === "closed"/);
  assert.match(source, /closed-gate probe not applicable/);
  assert.match(source, /args:\s*\["--acp-stdio"\]/);
  assert.match(source, /verifyAcpProtocolSmoke/);
  assert.match(source, /args:\s*\["--acp-self-test"\]/);
  assert.match(source, /v11FrozenSmokeTest/);
  assert.match(source, /\["--v11-self-test"\]/);
  assert.match(source, /report\.office_repair_prompt_sha256/);
  assert.match(source, /VERIFY_BUNDLE_V11_GATE_MODE/);
  assert.match(source, /report\.gate_mode !== expectedGateMode/);
  assert.match(source, /report\.gates_released !== expectedReleased/);
  assert.match(source, /authoritativeOfficeRendererSmokeTest/);
  assert.match(source, /\["--office-renderer-self-test"\]/);
  assert.match(source, /report\.schema_version !== 2/);
  assert.match(source, /report\.app_version !== expectedAppVersion/);
  assert.match(source, /report\.release_commit !== expectedReleaseCommit/);
  assert.match(source, /environment: \{\}/);
  assert.match(source, /report\.quality !== "authoritative"/);
  assert.match(source, /report\.native_closure_sha256/);
  assert.match(source, /report\.native_file_count < 2/);
  assert.match(source, /"dependency-manifest"/);
  assert.match(source, /"sandbox-manifest"/);
  assert.match(source, /report\.execution_probe/);
  assert.match(source, /executionProbe\.embedded_font_count < 1/);
  assert.match(source, /report\.native_sandbox_contract/);
  assert.match(source, /report\.native_sandbox_behavior/);
  assert.match(source, /nativeSandboxContract\.status !== "declared-not-proven"/);
  assert.match(source, /nativeSandboxContract\.native_behavior_proven !== false/);
  assert.match(source, /nativeSandboxContract\.adversarial_evidence_required !== true/);
  assert.match(source, /nativeSandboxBehavior\.status !== "proven"/);
  assert.match(source, /nativeSandboxBehavior\.native_behavior_proven !== true/);
  assert.match(source, /observedNativeSandboxCapabilities/);
  assert.match(source, /nativeSandboxBehavior\.launcher_sha256 !==/);
  assert.match(source, /nativeSandboxContract\.launcher_sha256/);
  assert.match(source, /VERIFY_BUNDLE_OFFICE_RENDERER_REPORT/);
  assert.match(source, /VERIFY_BUNDLE_OFFICE_RENDERER_PROFILE/);
  assert.match(source, /UNSIGNED_DEGRADED_PROFILE/);
  assert.match(source, /"windows-arm64": "windows-arm64"/);
  assert.match(source, /office-renderer-profile\.json/);
  assert.match(source, /suxiaoyou-office-renderer-profile-v1/);
  assert.match(source, /unsignedDegradedOfficeRendererSmokeTest/);
  assert.match(source, /unsigned-degraded bundle unexpectedly contains an Office renderer tree/);
  assert.match(source, /result\.status !== 1/);
  assert.match(source, /schema_version: 2, status: "unavailable"/);
  assert.match(source, /authoritative_authoring_available: false/);
  assert.match(source, /status: "degraded"/);
  assert.match(source, /writeJsonEvidenceReport/);
  assert.match(source, /report\.renderer_version !== `attestation-\$\{report\.attestation_sha256\}`/);
  assert.match(source, /VERIFY_BUNDLE_OFFICE_PLATFORM target/);
  assert.match(source, /V11_USER_OFFICE_TEMPLATES_BETA_RELEASED/);
});

test("closed verification rejects an Office renderer profile selection", (t) => {
  const dist = createCompleteStaticBundle(t);
  const result = verifyBundle(dist, {
    VERIFY_BUNDLE_OFFICE_RENDERER_PROFILE: "unsigned-degraded",
  });

  assert.notEqual(result.status, 0, result.stdout);
  assert.match(
    result.stderr,
    /closed v1\.1 bundle verification must not select an Office renderer profile/,
  );
  assert.doesNotMatch(result.stdout, /smoke test skipped/);
});

test("released verification rejects an unknown Office renderer profile", (t) => {
  const dist = createCompleteStaticBundle(t);
  const result = verifyBundle(dist, {
    VERIFY_BUNDLE_V11_GATE_MODE: "released",
    VERIFY_BUNDLE_OFFICE_RENDERER_PROFILE: "ambient",
  });

  assert.notEqual(result.status, 0, result.stdout);
  assert.match(
    result.stderr,
    /VERIFY_BUNDLE_OFFICE_RENDERER_PROFILE must be signed-authoritative or unsigned-degraded/,
  );
  assert.doesNotMatch(result.stderr, /cannot skip runtime smoke tests/);
});

test("released v1.1 bundle verification cannot skip runtime smoke", (t) => {
  const dist = createCompleteStaticBundle(t);
  const result = verifyBundle(dist, {
    VERIFY_BUNDLE_V11_GATE_MODE: "released",
  });

  assert.notEqual(result.status, 0, result.stdout);
  assert.match(
    result.stderr,
    /released v1\.1 bundle verification cannot skip runtime smoke tests/,
  );
  assert.doesNotMatch(result.stdout, /smoke test skipped/);
});

test("rejects a bundled Office repair prompt whose authenticated text drifted", (t) => {
  const dist = createCompleteStaticBundle(t);
  const target = join(
    dist,
    "_internal/app/agent/prompts/office_repair.txt",
  );
  writeFileSync(target, "tampered Office repair policy\n");

  const result = verifyBundle(dist);

  assert.notEqual(result.status, 0, result.stdout);
  assert.match(result.stderr, /file digest mismatch/);
  assert.match(result.stderr, new RegExp(escapeRegex(target)));
});

for (const requirement of REQUIRED_STATIC_ASSETS) {
  test(`still rejects a bundle missing ${requirement.relativePath}`, (t) => {
    const dist = createCompleteStaticBundle(t);
    const target = join(dist, requirement.relativePath);
    rmSync(target, { recursive: true, force: true });

    const result = verifyBundle(dist);

    assert.notEqual(result.status, 0, result.stdout);
    assert.match(result.stderr, new RegExp(escapeRegex(target)));
  });
}

for (const relativePath of [
  "_internal/app/channels/bridge",
  "_internal/app/data/skills_catalog.json",
  "_internal/app/data/skills/doc-coauthoring",
  "_internal/app/data/skills/docx/scripts",
  "_internal/app/data/skills/pdf/scripts",
  "_internal/app/data/skills/pptx/scripts",
  "_internal/app/data/skills/xlsx/scripts",
  "_internal/app/data/skills/canvas-design/design-philosophy-muse.md",
  "_internal/app/data/skills/canvas-design/muse-logo.png",
]) {
  test(`rejects removed or non-redistributable payload ${relativePath}`, (t) => {
    const dist = createCompleteStaticBundle(t);
    const target = join(dist, relativePath);
    mkdirSync(dirname(target), { recursive: true });
    if (relativePath.endsWith(".json") || relativePath.endsWith(".md") || relativePath.endsWith(".png")) {
      writeFileSync(target, "forbidden fixture\n");
    } else {
      mkdirSync(target, { recursive: true });
    }

    const result = verifyBundle(dist);

    assert.notEqual(result.status, 0, result.stdout);
    assert.match(result.stderr, new RegExp(escapeRegex(target)));
  });
}

for (const relativePath of [
  "_internal/_dbm.cpython-312-darwin.so",
  "_internal/adodbapi",
  "_internal/bidi",
  "_internal/svglib-2.0.2.dist-info",
  "_internal/xhtml2pdf",
  "_internal/pyhanko_certvalidator",
]) {
  test(`rejects removed or non-shipping component ${relativePath}`, (t) => {
    const dist = createCompleteStaticBundle(t);
    mkdirSync(join(dist, relativePath), { recursive: true });

    const result = verifyBundle(dist);

    assert.notEqual(result.status, 0, result.stdout);
    assert.match(result.stderr, new RegExp(escapeRegex(join(dist, relativePath))));
  });
}

function createCompleteStaticBundle(t) {
  const dist = mkdtempSync(join(tmpdir(), "verify-bundle-contract-"));
  t.after(() => rmSync(dist, { recursive: true, force: true }));

  for (const requirement of REQUIRED_STATIC_ASSETS) {
    const target = join(dist, requirement.relativePath);
    if (requirement.kind === "file") {
      mkdirSync(dirname(target), { recursive: true });
      const content = requirement.relativePath.endsWith("/office_repair.txt")
        ? readFileSync(officeRepairPromptSource)
        : "fixture\n";
      writeFileSync(target, content);
      continue;
    }

    mkdirSync(target, { recursive: true });
    if (requirement.kind === "nonempty-dir") {
      writeFileSync(join(target, ".fixture"), "fixture\n");
    }
  }

  return dist;
}

function verifyBundle(dist, environment = {}) {
  return spawnSync(process.execPath, [verifyBundleScript, dist], {
    encoding: "utf8",
    env: {
      ...process.env,
      VERIFY_BUNDLE_SKIP_SMOKE: "1",
      ...environment,
    },
  });
}

function escapeRegex(value) {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}
