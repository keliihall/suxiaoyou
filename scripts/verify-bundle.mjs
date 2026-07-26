#!/usr/bin/env node
/**
 * Verify a PyInstaller bundle of the 苏小有 backend.
 *
 * This is the single source of truth for "what must ship inside
 * backend/dist/suxiaoyou-backend/" — shared by local dev and CI so the
 * two can never drift.
 *
 * Usage:
 *   node scripts/verify-bundle.mjs [dist-dir]
 *   node scripts/verify-bundle.mjs backend/dist/suxiaoyou-backend
 *   node scripts/verify-bundle.mjs path/to/苏小有.app/Contents/Resources/backend
 *
 * Exits non-zero (with a loud message) if anything critical is missing.
 * Why this exists: 1.0.7 shipped without `frontend_out` because the
 * PyInstaller spec silently filtered missing paths, so the mobile PWA
 * over cloudflare tunnel returned 404. Never let that happen again.
 */

import {
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  readdirSync,
  renameSync,
  rmSync,
  statSync,
  writeFileSync,
} from "node:fs";
import { createHash } from "node:crypto";
import { dirname, join, resolve } from "node:path";
import { tmpdir } from "node:os";
import { argv, env, exit, platform } from "node:process";
import { spawn, spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import {
  BackendSmokeError,
  resolveStartupTimeoutMs,
  runBackendSmoke,
} from "./verify-bundle-smoke.mjs";
import {
  resolveCheckoutCommit,
  validateOfficeContractReport,
} from "./office-contract-evidence.mjs";
import {
  verifyAcpClosedGate,
  verifyAcpProtocolSmoke,
} from "./verify-acp-bundle.mjs";

const distArg = argv[2] ?? "backend/dist/suxiaoyou-backend";
const dist = resolve(distArg);
const repositoryRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");

if (!existsSync(dist)) {
  fail(`bundle directory does not exist: ${dist}`);
}

const internal = join(dist, "_internal");
const exeName = platform === "win32" ? "suxiaoyou-backend.exe" : "suxiaoyou-backend";
const OFFICE_REPAIR_PROMPT_SHA256 =
  "7c9cd1613c47761539cd04fa22634e467881bac9917f5c88018454d5b91b5272";
const V11_GATE_NAMES = Object.freeze([
  "V11_CHECKPOINTS_RELEASED",
  "V11_REWIND_RELEASED",
  "V11_HOOKS_RELEASED",
  "V11_ACP_RELEASED",
  "V11_WORKTREES_RELEASED",
  "V11_VALIDATION_AGENT_RELEASED",
  "V11_OFFICE_V2_RELEASED",
  "V11_USER_OFFICE_TEMPLATES_BETA_RELEASED",
]);
const V11_CAPABILITY_NAMES = Object.freeze([
  "checkpoints",
  "rewind",
  "hooks",
  "acp",
  "worktrees",
  "validator",
  "office_preview",
  "office_authoring",
  "user_office_templates",
]);
const SIGNED_AUTHORITATIVE_PROFILE = "signed-authoritative";
const UNSIGNED_DEGRADED_PROFILE = "unsigned-degraded";

/**
 * Each entry describes one required asset. `kind` is "file" | "dir"
 * | "nonempty-dir". Text policies may additionally carry an
 * `expectedTextSha256` over canonical LF UTF-8. Add new entries as new
 * resources are bundled — CI will start failing until reality matches the
 * contract.
 */
const required = [
  { kind: "file", path: join(dist, exeName), why: "backend launcher" },

  { kind: "dir", path: internal, why: "PyInstaller _internal/ tree" },

  // Alembic (DB migrations run at startup)
  { kind: "nonempty-dir", path: join(internal, "alembic"), why: "DB migrations" },
  { kind: "file", path: join(internal, "alembic.ini"), why: "alembic config" },
  {
    kind: "file",
    path: join(
      internal,
      "alembic",
      "versions",
      "0010_v110_checkpoint_ledger.py",
    ),
    why: "v1.1 checkpoint ledger migration",
  },
  {
    kind: "file",
    path: join(
      internal,
      "alembic",
      "versions",
      "0011_v110_user_office_templates.py",
    ),
    why: "v1.1 user Office template migration",
  },
  {
    kind: "file",
    path: join(
      internal,
      "alembic",
      "versions",
      "0012_v110_workspace_identity_v2.py",
    ),
    why: "v1.1 workspace identity v2 release boundary",
  },

  // Agent prompt templates
  {
    kind: "nonempty-dir",
    path: join(internal, "app", "agent", "prompts"),
    why: "agent prompt templates",
  },
  {
    kind: "file",
    path: join(internal, "app", "agent", "prompts", "validator.txt"),
    why: "server-owned read-only Validator policy",
  },
  {
    kind: "file",
    path: join(internal, "app", "agent", "prompts", "office_repair.txt"),
    why: "hash-locked capability-free Office repair policy",
    expectedTextSha256: OFFICE_REPAIR_PROMPT_SHA256,
  },

  // Bundled data (skills/plugins/connectors)
  {
    kind: "file",
    path: join(internal, "app", "data", "connectors.json"),
    why: "connectors registry",
  },
  {
    kind: "dir",
    path: join(internal, "app", "data", "skills"),
    why: "builtin skills",
  },
  {
    kind: "dir",
    path: join(internal, "app", "data", "plugins"),
    why: "builtin plugins",
  },
  {
    kind: "file",
    path: join(
      internal,
      "app",
      "data",
      "fonts",
      "SuxiaoyouCJK-Regular.ttf",
    ),
    why: "embedded CJK font for portable PDF export",
  },
  {
    kind: "file",
    path: join(internal, "app", "data", "fonts", "OFL-1.1.txt"),
    why: "embedded PDF font license",
  },
  {
    kind: "file",
    path: join(internal, "app", "data", "fonts", "PROVENANCE.md"),
    why: "embedded PDF font provenance",
  },

  // Signed first-party Office templates. Check every release-bound file so a
  // partial catalog can never pass the desktop bundle gate.
  {
    kind: "file",
    path: join(internal, "app", "office_templates", "assets", "catalog.json"),
    why: "signed Office template catalog",
  },
  {
    kind: "file",
    path: join(
      internal,
      "app",
      "office_templates",
      "assets",
      "catalog.sig.json",
    ),
    why: "Office template catalog signature",
  },
  ...[
    "business-brief.docx",
    "project-tracker.xlsx",
    "status-update.pptx",
  ].map((filename) => ({
    kind: "file",
    path: join(
      internal,
      "app",
      "office_templates",
      "assets",
      "templates",
      filename,
    ),
    why: `first-party Office template ${filename}`,
  })),

  // Frontend static export — served by FastAPI at /m for mobile PWA.
  // Without these files, cloudflare-tunnel remote access is broken
  // while the desktop UI continues to work (Tauri serves its own copy).
  {
    kind: "dir",
    path: join(internal, "frontend_out"),
    why: "mobile PWA static export (remote access over tunnel)",
  },
  {
    kind: "file",
    path: join(internal, "frontend_out", "m.html"),
    why: "mobile PWA entry point — /m over tunnel",
  },
  {
    kind: "file",
    path: join(internal, "frontend_out", "index.html"),
    why: "frontend root",
  },
  {
    kind: "nonempty-dir",
    path: join(internal, "frontend_out", "_next", "static"),
    why: "Next.js static chunks — without these the PWA won't boot",
  },

  // Critical Python packages that MUST be inside the bundle as
  // extracted top-level directories. These catch the "PyInstaller ran
  // against the wrong python env" failure mode: the spec's collect_all()
  // silently returns empty when the package isn't installed, so the
  // build technically succeeds but the backend crashes at startup with
  // "No module named 'uvicorn'". Without these checks verify-bundle
  // would happily pass a dead bundle.
  //
  // Only packages guaranteed to produce extracted C extensions or data land
  // here. Pure-Python modules can be packed into PYZ-00.pyz and are not
  // visible as directories; the runtime smoke test below covers them.
  //
  // In particular, SQLAlchemy 2.0.46 ships a py3-none-any wheel on Intel
  // macOS, so a valid PyInstaller bundle has no _internal/sqlalchemy/. The
  // smoke test starts the FastAPI lifespan, which creates the SQLAlchemy
  // engine and runs create_all/auto-migration before /m can return its
  // expected authentication response.
  {
    kind: "nonempty-dir",
    path: join(internal, "uvicorn"),
    why: "ASGI server — has data files via collect_all",
  },
  {
    kind: "nonempty-dir",
    path: join(internal, "pydantic_core"),
    why: "pydantic Rust core — separate from pure-python pydantic",
  },
  {
    kind: "nonempty-dir",
    path: join(internal, "app"),
    why: "application code",
  },
];

const forbidden = [
  {
    path: join(internal, "app", "channels", "bridge"),
    why: "unfinished WhatsApp bridge must not be included in release bundles",
  },
  {
    path: join(internal, "app", "data", "skills_catalog.json"),
    why: "unreviewed remote skill catalog must not be included in release bundles",
  },
  {
    path: join(internal, "app", "data", "skills", "doc-coauthoring"),
    why: "non-redistributable legacy document skill was removed",
  },
  ...["docx", "pdf", "pptx", "xlsx"].map((skill) => ({
    path: join(internal, "app", "data", "skills", skill, "scripts"),
    why: `legacy ${skill} implementation scripts must not be redistributed`,
  })),
  {
    path: join(
      internal,
      "app",
      "data",
      "skills",
      "canvas-design",
      "design-philosophy-muse.md",
    ),
    why: "removed legacy branded canvas asset",
  },
  {
    path: join(
      internal,
      "app",
      "data",
      "skills",
      "canvas-design",
      "muse-logo.png",
    ),
    why: "removed legacy branded canvas asset",
  },
];

const problems = [];
for (const req of required) {
  if (!existsSync(req.path)) {
    problems.push(`missing ${req.kind}: ${req.path}  (${req.why})`);
    continue;
  }
  const st = statSync(req.path);
  if (req.kind === "file" && !st.isFile()) {
    problems.push(`not a file: ${req.path}  (${req.why})`);
  } else if (req.kind === "file" && req.expectedTextSha256) {
    const actualSha256 = canonicalTextSha256(req.path);
    if (actualSha256 !== req.expectedTextSha256) {
      problems.push(`file digest mismatch: ${req.path}  (${req.why})`);
    }
  } else if ((req.kind === "dir" || req.kind === "nonempty-dir") && !st.isDirectory()) {
    problems.push(`not a directory: ${req.path}  (${req.why})`);
  } else if (req.kind === "nonempty-dir" && readdirSync(req.path).length === 0) {
    problems.push(`empty directory: ${req.path}  (${req.why})`);
  }
}

for (const item of forbidden) {
  if (existsSync(item.path)) {
    problems.push(`unexpected bundled path: ${item.path}  (${item.why})`);
  }
}

const forbiddenComponentNames = [
  { pattern: /^_dbm(?:$|[-_.])/i, why: "unused Berkeley DB extension" },
  { pattern: /^adodbapi(?:$|[-_.])/i, why: "unused pywin32 LGPL adodbapi component" },
  { pattern: /^(?:bidi|python[-_.]bidi)(?:$|[-_.])/i, why: "removed python-bidi dependency" },
  { pattern: /^svglib(?:$|[-_.])/i, why: "removed svglib dependency" },
  { pattern: /^xhtml2pdf(?:$|[-_.])/i, why: "removed xhtml2pdf dependency" },
  { pattern: /^pyhanko(?:$|[-_.])/i, why: "removed pyHanko dependency" },
];

for (const item of forbiddenComponentNames) {
  const match = findEntryByName(internal, item.pattern);
  if (match) {
    problems.push(`unexpected bundled component: ${match}  (${item.why})`);
  }
}

if (problems.length > 0) {
  console.error("\n[verify-bundle] Bundle is INCOMPLETE — refusing to ship:");
  for (const p of problems) console.error(`  ✗ ${p}`);
  console.error(
    "\nThis is the guard that would have caught the 1.0.7 remote-access\n" +
      "regression. Fix the build (suxiaoyou.spec + frontend next build) and\n" +
      "re-run before uploading any artifacts.\n",
  );
  exit(1);
}

console.log(`[verify-bundle] static: ${required.length} required assets present in ${dist}`);

// ── Runtime smoke test ───────────────────────────────────────────────
//
// Static checks can't tell whether pure-python packages that live
// inside PYZ-00.pyz (fastapi, starlette, pydantic, …) made it in, nor
// whether the binary actually boots. Launch it on a throwaway port,
// probe /health and /m, then kill it. If it crashes with a missing
// import we catch it here — not in the wild.
//
// Skip with VERIFY_BUNDLE_SKIP_SMOKE=1 on hosts that can't execute
// the target binary (e.g. cross-compiled artifacts inspected on a
// different OS).

const expectedV11GateMode = String(
  env.VERIFY_BUNDLE_V11_GATE_MODE ?? "closed",
).trim();
if (!new Set(["closed", "released"]).has(expectedV11GateMode)) {
  fail(
    `VERIFY_BUNDLE_V11_GATE_MODE must be closed or released, got ${expectedV11GateMode || "empty"}`,
  );
}
const configuredOfficeRendererProfile = String(
  env.VERIFY_BUNDLE_OFFICE_RENDERER_PROFILE ?? "",
).trim();
let expectedOfficeRendererProfile = null;
if (expectedV11GateMode === "released") {
  expectedOfficeRendererProfile =
    configuredOfficeRendererProfile || SIGNED_AUTHORITATIVE_PROFILE;
  if (
    !new Set([
      SIGNED_AUTHORITATIVE_PROFILE,
      UNSIGNED_DEGRADED_PROFILE,
    ]).has(expectedOfficeRendererProfile)
  ) {
    fail(
      "VERIFY_BUNDLE_OFFICE_RENDERER_PROFILE must be " +
        `${SIGNED_AUTHORITATIVE_PROFILE} or ${UNSIGNED_DEGRADED_PROFILE}`,
    );
  }
} else if (configuredOfficeRendererProfile) {
  fail("closed v1.1 bundle verification must not select an Office renderer profile");
}

if (env.VERIFY_BUNDLE_SKIP_SMOKE === "1") {
  if (expectedV11GateMode === "released") {
    fail(
      "released v1.1 bundle verification cannot skip runtime smoke tests",
    );
  }
  console.log("[verify-bundle] smoke test skipped (VERIFY_BUNDLE_SKIP_SMOKE=1)");
  exit(0);
}

const binary = join(dist, exeName);
const port = 17000 + Math.floor(Math.random() * 500);

if (expectedV11GateMode === "closed") {
  acpClosedGateSmokeTest(binary);
} else {
  console.log(
    "[verify-bundle] ACP smoke: released production gate; closed-gate probe not applicable",
  );
}
await acpProtocolSmokeTest(binary);
v11FrozenSmokeTest(
  binary,
  expectedV11GateMode,
  expectedOfficeRendererProfile,
);
providerSmokeTest(binary);
officeSmokeTest(binary);
sandboxSmokeTest(binary);
await smokeTest(binary, port);

function acpClosedGateSmokeTest(bin) {
  console.log("[verify-bundle] ACP smoke: production stdio entry fails closed");
  try {
    verifyAcpClosedGate({
      command: bin,
      args: ["--acp-stdio"],
      cwd: dist,
      environment: env,
    });
  } catch (error) {
    fail(error instanceof Error ? error.message : String(error));
  }
}

async function acpProtocolSmokeTest(bin) {
  console.log(
    "[verify-bundle] ACP smoke: initialize, synthetic session, and cancellation over stdio",
  );
  try {
    const report = await verifyAcpProtocolSmoke({
      command: bin,
      args: ["--acp-self-test"],
      cwd: dist,
      environment: env,
    });
    if (
      report.protocolVersion !== 1 ||
      report.sessionId !== "bundle-smoke-session" ||
      report.stopReason !== "cancelled" ||
      report.frameCount < 4
    ) {
      fail(`ACP protocol smoke returned an invalid report: ${JSON.stringify(report)}`);
    }
  } catch (error) {
    fail(error instanceof Error ? error.message : String(error));
  }
}

function providerSmokeTest(bin) {
  console.log("[verify-bundle] provider smoke: official Anthropic/Gemini constructors");
  const report = runJsonCommand(bin, ["--provider-self-test"], 30_000);
  if (
    report.status !== "ok" ||
    JSON.stringify(report.providers) !== JSON.stringify(["anthropic", "google"])
  ) {
    fail(`provider smoke returned an invalid report: ${JSON.stringify(report)}`);
  }
}

function v11FrozenSmokeTest(bin, expectedGateMode, expectedRendererProfile) {
  console.log(
    `[verify-bundle] v1.1 smoke: ${expectedGateMode} gate graph, modules, and signed assets`,
  );
  const report = runJsonCommand(bin, ["--v11-self-test"], 60_000);
  const expectedTemplates = [
    "business-brief@1.0.0",
    "project-tracker@1.0.0",
    "status-update@1.0.0",
  ];
  const expectedReleased = expectedGateMode === "released";
  const gateValues =
    report.gate_values && typeof report.gate_values === "object"
      ? report.gate_values
      : {};
  const capabilities =
    report.capabilities && typeof report.capabilities === "object"
      ? report.capabilities
      : {};
  const gateKeys = Object.keys(gateValues).sort();
  const capabilityKeys = Object.keys(capabilities).sort();
  const gatesMatch =
    JSON.stringify(gateKeys) === JSON.stringify([...V11_GATE_NAMES].sort()) &&
    V11_GATE_NAMES.every((name) => gateValues[name] === expectedReleased);
  const capabilitiesMatch =
    JSON.stringify(capabilityKeys) ===
      JSON.stringify([...V11_CAPABILITY_NAMES].sort()) &&
    V11_CAPABILITY_NAMES.every((name) => {
      const status = capabilities[name];
      return (
        status &&
        typeof status === "object" &&
        status.code_gate === expectedReleased &&
        status.released === expectedReleased &&
        Array.isArray(status.dependencies) &&
        Array.isArray(status.missing_dependencies) &&
        (!expectedReleased || status.missing_dependencies.length === 0)
      );
    });
  if (
    report.status !== "ok" ||
    report.gate_mode !== expectedGateMode ||
    report.gates_closed !== !expectedReleased ||
    report.gates_released !== expectedReleased ||
    !gatesMatch ||
    !capabilitiesMatch ||
    !Number.isInteger(report.module_count) ||
    report.module_count < 50 ||
    JSON.stringify(report.templates) !== JSON.stringify(expectedTemplates) ||
    report.office_repair_prompt_sha256 !== OFFICE_REPAIR_PROMPT_SHA256
  ) {
    fail(`v1.1 frozen smoke returned an invalid report: ${JSON.stringify(report)}`);
  }
  if (!expectedReleased) return;
  verifyOfficeRendererProfileMarker(expectedRendererProfile);
  if (expectedRendererProfile === SIGNED_AUTHORITATIVE_PROFILE) {
    authoritativeOfficeRendererSmokeTest(bin);
  } else {
    unsignedDegradedOfficeRendererSmokeTest(bin);
  }
}

function expectedOfficePlatformTarget() {
  const officePlatform = String(env.VERIFY_BUNDLE_OFFICE_PLATFORM ?? "").trim();
  const platformTargets = {
    "windows-x64": "windows-x64",
    "windows-arm64": "windows-arm64",
    "macos-arm64": "darwin-arm64",
    "macos-x64": "darwin-x64",
    "linux-x64": "linux-x64",
    "linux-arm64": "linux-arm64",
  };
  const expectedTarget = platformTargets[officePlatform];
  if (!expectedTarget) {
    fail(
      "released v1.1 bundle verification requires an exact " +
        "VERIFY_BUNDLE_OFFICE_PLATFORM target",
    );
  }
  return expectedTarget;
}

function expectedReleaseIdentity() {
  const packageMetadata = JSON.parse(
    readFileSync(join(repositoryRoot, "package.json"), "utf8"),
  );
  const appVersion = String(packageMetadata.version ?? "");
  if (!/^(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)$/u.test(appVersion)) {
    fail("released v1.1 bundle verification requires an X.Y.Z package version");
  }
  return {
    appVersion,
    releaseCommit: resolveCheckoutCommit({
      cwd: repositoryRoot,
      environment: {},
    }),
  };
}

function verifyOfficeRendererProfileMarker(expectedProfile) {
  const markerPath = join(
    internal,
    "app",
    "data",
    "office-renderer-profile.json",
  );
  let raw;
  let marker;
  try {
    raw = readFileSync(markerPath, "utf8");
    marker = JSON.parse(raw);
  } catch {
    fail("released v1.1 bundle has no valid Office renderer profile marker");
  }
  const { appVersion, releaseCommit } = expectedReleaseIdentity();
  const authoritativeRendererBundled =
    expectedProfile === SIGNED_AUTHORITATIVE_PROFILE;
  const expected = {
    app_version: appVersion,
    authoritative_authoring_available: false,
    authoritative_renderer_bundled: authoritativeRendererBundled,
    contract: "suxiaoyou-office-renderer-profile-v1",
    profile: expectedProfile,
    release_commit: releaseCommit,
    schema_version: 1,
  };
  const canonical = `${JSON.stringify(expected)}\n`;
  if (raw !== canonical || JSON.stringify(marker) !== JSON.stringify(expected)) {
    fail("Office renderer profile marker does not match this release checkout");
  }
  return {
    ...expected,
    marker_sha256: createHash("sha256").update(raw).digest("hex"),
  };
}

function parseLastJsonLine(stdout, label) {
  const lines = String(stdout || "")
    .trim()
    .split(/\r?\n/u)
    .reverse();
  for (const line of lines) {
    try {
      const report = JSON.parse(line);
      if (report && typeof report === "object" && !Array.isArray(report)) {
        return report;
      }
    } catch {
      // Frozen/native dependencies may emit non-JSON diagnostics before the report.
    }
  }
  fail(`${label} did not emit a JSON report`);
}

function unsignedDegradedOfficeRendererSmokeTest(bin) {
  const expectedTarget = expectedOfficePlatformTarget();
  const marker = verifyOfficeRendererProfileMarker(UNSIGNED_DEGRADED_PROFILE);
  const rendererRoot = join(internal, "app", "data", "office-renderer");
  if (existsSync(rendererRoot)) {
    fail("unsigned-degraded bundle unexpectedly contains an Office renderer tree");
  }
  console.log(
    `[verify-bundle] Office renderer smoke: ${UNSIGNED_DEGRADED_PROFILE}; authoritative authoring unavailable`,
  );
  const result = spawnSync(bin, ["--office-renderer-self-test"], {
    encoding: "utf8",
    timeout: 30_000,
    env,
    windowsHide: true,
  });
  if (result.error) {
    fail(`degraded Office renderer self-test failed to launch: ${result.error.message}`);
  }
  const selfTest = parseLastJsonLine(
    result.stdout,
    "degraded Office renderer self-test",
  );
  if (
    result.status !== 1 ||
    JSON.stringify(selfTest) !==
      JSON.stringify({ schema_version: 2, status: "unavailable" })
  ) {
    fail(
      "unsigned-degraded Office renderer self-test did not prove unavailability: " +
        JSON.stringify(selfTest),
    );
  }
  const report = {
    app_version: marker.app_version,
    authoritative_authoring_available: false,
    authoritative_renderer_bundled: false,
    available: false,
    marker_sha256: marker.marker_sha256,
    platform_target: expectedTarget,
    profile: UNSIGNED_DEGRADED_PROFILE,
    quality: "unavailable",
    release_commit: marker.release_commit,
    schema_version: 1,
    status: "degraded",
  };
  writeJsonEvidenceReport(
    report,
    "VERIFY_BUNDLE_OFFICE_RENDERER_REPORT",
    "degraded Office renderer evidence",
  );
}

function authoritativeOfficeRendererSmokeTest(bin) {
  const expectedTarget = expectedOfficePlatformTarget();
  console.log(
    `[verify-bundle] Office renderer smoke: signed authoritative ${expectedTarget} deployment`,
  );
  const {
    appVersion: expectedAppVersion,
    releaseCommit: expectedReleaseCommit,
  } = expectedReleaseIdentity();
  // Release identity is derived from the checkout itself. In particular, an
  // environment override accepted by legacy evidence tooling must not decide
  // which frozen application identity can authorize Office writes.
  const report = runJsonCommand(bin, ["--office-renderer-self-test"], 180_000);
  const expectedFields = [
    "app_version",
    "attestation_sha256",
    "available",
    "bundle_tree_sha256",
    "component_count",
    "components",
    "execution_probe",
    "font_digest",
    "font_tree_sha256",
    "native_closure_sha256",
    "native_dependency_count",
    "native_file_count",
    "native_sandbox_behavior",
    "native_sandbox_contract",
    "platform_target",
    "quality",
    "release_commit",
    "renderer_id",
    "renderer_version",
    "schema_version",
    "status",
  ];
  const expectedComponents = [
    "bundle-tree",
    "dependency-manifest",
    "font-manifest",
    "license-manifest",
    "pdftoppm",
    "sandbox-manifest",
    "soffice",
  ];
  const sha256 = /^(?!0{64}$)[0-9a-f]{64}$/;
  const sandboxByFamily = {
    darwin: {
      contractId: "suxiaoyou.office-sandbox.macos-app-sandbox-xpc.v1",
      capabilities: [
        "app_sandbox",
        "host_filesystem_read_only",
        "network_denied",
        "private_input_read_only",
        "private_output_write_only",
        "process_tree_contained",
        "xpc_service",
      ],
    },
    linux: {
      contractId: "suxiaoyou.office-sandbox.linux-namespaces-seccomp-cgroup.v1",
      capabilities: [
        "cgroup",
        "host_filesystem_read_only",
        "mount_namespace",
        "network_denied",
        "network_namespace",
        "private_input_read_only",
        "private_output_write_only",
        "process_tree_contained",
        "seccomp",
        "user_namespace",
      ],
    },
    windows: {
      contractId: "suxiaoyou.office-sandbox.windows-appcontainer-restricted-token.v1",
      capabilities: [
        "app_container",
        "host_filesystem_read_only",
        "kill_on_close_job",
        "network_denied",
        "private_input_read_only",
        "private_output_write_only",
        "process_tree_contained",
        "restricted_token",
      ],
    },
  };
  const expectedSandbox = sandboxByFamily[expectedTarget.split("-", 1)[0]];
  const nativeSandboxContract = report.native_sandbox_contract;
  const expectedNativeSandboxContractFields = [
    "adversarial_evidence_required",
    "bundle_tree_sha256",
    "capabilities",
    "contract_id",
    "dependency_manifest_sha256",
    "launcher_sha256",
    "native_behavior_proven",
    "platform_target",
    "sandbox_manifest_sha256",
    "schema_version",
    "status",
  ];
  const nativeSandboxBehavior = report.native_sandbox_behavior;
  const expectedNativeSandboxBehaviorFields = [
    "attempts_sha256",
    "bundle_tree_sha256",
    "capabilities",
    "contract_id",
    "dependency_manifest_sha256",
    "evidence_sha256",
    "helper_sha256",
    "launcher_sha256",
    "native_behavior_proven",
    "nonce_sha256",
    "output_proof_sha256",
    "platform_target",
    "sandbox_manifest_sha256",
    "schema_version",
    "status",
  ];
  const observedNativeSandboxCapabilities = new Set([
    "host_filesystem_read_only",
    "network_denied",
    "private_input_read_only",
    "private_output_write_only",
    "process_tree_contained",
  ]);
  const executionProbe = report.execution_probe;
  const expectedExecutionProbeFields = [
    "bundle_tree_sha256",
    "embedded_font_count",
    "page_count",
    "pages",
    "pdf_sha256",
    "probe_manifest_sha256",
    "probe_source_sha256",
    "render_manifest_sha256",
    "schema_version",
  ];
  if (
    JSON.stringify(Object.keys(report).sort()) !== JSON.stringify(expectedFields) ||
    report.schema_version !== 2 ||
    report.status !== "ok" ||
    report.available !== true ||
    report.quality !== "authoritative" ||
    report.app_version !== expectedAppVersion ||
    report.release_commit !== expectedReleaseCommit ||
    report.platform_target !== expectedTarget ||
    report.renderer_id !== "suxiaoyou-attested-office" ||
    !sha256.test(report.font_digest) ||
    !sha256.test(report.font_tree_sha256) ||
    !sha256.test(report.attestation_sha256) ||
    !sha256.test(report.bundle_tree_sha256) ||
    !sha256.test(report.native_closure_sha256) ||
    !Number.isInteger(report.native_file_count) ||
    report.native_file_count < 2 ||
    !Number.isInteger(report.native_dependency_count) ||
    report.native_dependency_count < 0 ||
    report.renderer_version !== `attestation-${report.attestation_sha256}` ||
    report.component_count !== expectedComponents.length ||
    JSON.stringify(report.components) !== JSON.stringify(expectedComponents) ||
    !nativeSandboxContract ||
    JSON.stringify(Object.keys(nativeSandboxContract).sort()) !==
      JSON.stringify(expectedNativeSandboxContractFields) ||
    nativeSandboxContract.schema_version !== 1 ||
    nativeSandboxContract.status !== "declared-not-proven" ||
    nativeSandboxContract.platform_target !== expectedTarget ||
    nativeSandboxContract.contract_id !== expectedSandbox.contractId ||
    JSON.stringify(nativeSandboxContract.capabilities) !==
      JSON.stringify(expectedSandbox.capabilities) ||
    !sha256.test(nativeSandboxContract.sandbox_manifest_sha256) ||
    !sha256.test(nativeSandboxContract.dependency_manifest_sha256) ||
    !sha256.test(nativeSandboxContract.launcher_sha256) ||
    nativeSandboxContract.bundle_tree_sha256 !== report.bundle_tree_sha256 ||
    nativeSandboxContract.native_behavior_proven !== false ||
    nativeSandboxContract.adversarial_evidence_required !== true ||
    !nativeSandboxBehavior ||
    JSON.stringify(Object.keys(nativeSandboxBehavior).sort()) !==
      JSON.stringify(expectedNativeSandboxBehaviorFields) ||
    nativeSandboxBehavior.schema_version !== 1 ||
    nativeSandboxBehavior.status !== "proven" ||
    nativeSandboxBehavior.native_behavior_proven !== true ||
    nativeSandboxBehavior.platform_target !== nativeSandboxContract.platform_target ||
    nativeSandboxBehavior.contract_id !== nativeSandboxContract.contract_id ||
    nativeSandboxBehavior.bundle_tree_sha256 !== report.bundle_tree_sha256 ||
    nativeSandboxBehavior.bundle_tree_sha256 !==
      nativeSandboxContract.bundle_tree_sha256 ||
    nativeSandboxBehavior.sandbox_manifest_sha256 !==
      nativeSandboxContract.sandbox_manifest_sha256 ||
    nativeSandboxBehavior.dependency_manifest_sha256 !==
      nativeSandboxContract.dependency_manifest_sha256 ||
    nativeSandboxBehavior.launcher_sha256 !==
      nativeSandboxContract.launcher_sha256 ||
    !sha256.test(nativeSandboxBehavior.helper_sha256) ||
    !sha256.test(nativeSandboxBehavior.nonce_sha256) ||
    !sha256.test(nativeSandboxBehavior.attempts_sha256) ||
    !sha256.test(nativeSandboxBehavior.output_proof_sha256) ||
    !sha256.test(nativeSandboxBehavior.evidence_sha256) ||
    !nativeSandboxBehavior.capabilities ||
    typeof nativeSandboxBehavior.capabilities !== "object" ||
    Array.isArray(nativeSandboxBehavior.capabilities) ||
    JSON.stringify(Object.keys(nativeSandboxBehavior.capabilities).sort()) !==
      JSON.stringify([...expectedSandbox.capabilities].sort()) ||
    expectedSandbox.capabilities.some(
      (name) =>
        nativeSandboxBehavior.capabilities[name] !==
        observedNativeSandboxCapabilities.has(name),
    ) ||
    !executionProbe ||
    JSON.stringify(Object.keys(executionProbe).sort()) !==
      JSON.stringify(expectedExecutionProbeFields) ||
    executionProbe.schema_version !== 1 ||
    executionProbe.bundle_tree_sha256 !== report.bundle_tree_sha256 ||
    !sha256.test(executionProbe.probe_manifest_sha256) ||
    !sha256.test(executionProbe.probe_source_sha256) ||
    !sha256.test(executionProbe.render_manifest_sha256) ||
    !sha256.test(executionProbe.pdf_sha256) ||
    !Number.isInteger(executionProbe.page_count) ||
    executionProbe.page_count < 1 ||
    !Number.isInteger(executionProbe.embedded_font_count) ||
    executionProbe.embedded_font_count < 1 ||
    !Array.isArray(executionProbe.pages) ||
    executionProbe.pages.length !== executionProbe.page_count ||
    executionProbe.pages.some(
      (page, index) =>
        !page ||
        JSON.stringify(Object.keys(page).sort()) !==
          JSON.stringify(["height_px", "page_number", "pixel_sha256", "width_px"]) ||
        page.page_number !== index + 1 ||
        !Number.isInteger(page.width_px) ||
        page.width_px < 1 ||
        !Number.isInteger(page.height_px) ||
        page.height_px < 1 ||
        !sha256.test(page.pixel_sha256),
    )
  ) {
    fail(
      `authoritative Office renderer smoke returned an invalid report: ${JSON.stringify(report)}`,
    );
  }
  writeJsonEvidenceReport(
    report,
    "VERIFY_BUNDLE_OFFICE_RENDERER_REPORT",
    "Office renderer evidence",
  );
}

function officeSmokeTest(bin) {
  const expectedPlatform = String(env.VERIFY_BUNDLE_OFFICE_PLATFORM ?? "").trim();
  const expectedCommit = resolveCheckoutCommit();
  const args = ["--office-self-test"];
  if (expectedPlatform) args.push(expectedPlatform);
  console.log(
    `[verify-bundle] Office smoke: restricted create/edit/reopen${
      expectedPlatform ? ` on ${expectedPlatform}` : ""
    }`,
  );
  const report = runJsonCommand(bin, args, 120_000, {
    SUXIAOYOU_RELEASE_COMMIT: expectedCommit,
  });
  const validation = validateOfficeContractReport(report, {
    expectedPlatform: expectedPlatform || undefined,
    expectedCommit,
    expectedReleaseRef: env.GITHUB_REF_NAME || undefined,
    requireFrozen: true,
  });
  if (!validation.ok) {
    fail(`Office smoke returned invalid evidence:\n- ${validation.failures.join("\n- ")}`);
  }

  writeJsonEvidenceReport(
    report,
    "VERIFY_BUNDLE_OFFICE_REPORT",
    "Office evidence",
  );
}

function writeJsonEvidenceReport(report, environmentName, label) {
  const outputPath = String(env[environmentName] ?? "").trim();
  if (outputPath) {
    const destination = resolve(outputPath);
    mkdirSync(dirname(destination), { recursive: true });
    const temporary = `${destination}.tmp-${process.pid}`;
    writeFileSync(temporary, `${JSON.stringify(report, null, 2)}\n`, {
      encoding: "utf8",
      mode: 0o600,
    });
    renameSync(temporary, destination);
    console.log(`[verify-bundle] ${label}: ${destination}`);
  }
}

function sandboxSmokeTest(bin) {
  const workspace = mkdtempSync(join(tmpdir(), "suxiaoyou-sandbox-smoke-"));
  try {
    console.log(`[verify-bundle] sandbox smoke: ${bin} --sandbox-self-test ${workspace}`);
    const report = runJsonCommand(bin, ["--sandbox-self-test", workspace], 120_000);
    if (report.status !== "ok" || report.platform !== platform) {
      fail(`sandbox smoke did not execute on ${platform}: ${JSON.stringify(report)}`);
    }
    for (const field of [
      "environment_sanitized",
      "descendant_terminated",
      "process_tree_reaped",
    ]) {
      if (report[field] !== true) {
        fail(`sandbox smoke did not prove ${field}: ${JSON.stringify(report)}`);
      }
    }
    if (platform === "win32") {
      if (
        report.sandbox !== "windows-job-object" ||
        report.workspace_execution !== "direct-approved" ||
        report.filesystem_isolated !== false ||
        report.network_isolated !== false
      ) {
        fail(`Windows execution contract was misreported: ${JSON.stringify(report)}`);
      }
    } else {
      for (const field of ["filesystem_isolated", "network_isolated"]) {
        if (report[field] !== true) {
          fail(`sandbox smoke did not prove ${field}: ${JSON.stringify(report)}`);
        }
      }
    }
  } finally {
    rmSync(workspace, { recursive: true, force: true });
  }
}

function runJsonCommand(bin, args, timeout, extraEnvironment = {}) {
  const result = spawnSync(bin, args, {
    encoding: "utf8",
    timeout,
    env: { ...env, ...extraEnvironment },
    windowsHide: true,
  });
  if (result.error) {
    fail(`command smoke failed to launch ${args[0]}: ${result.error.message}`);
  }
  if (result.status !== 0) {
    fail(
      `command smoke ${args[0]} exited ${result.status ?? result.signal}:\n` +
        `${result.stderr || result.stdout}`.slice(-4000),
    );
  }
  const lines = String(result.stdout || "")
    .trim()
    .split(/\r?\n/)
    .reverse();
  for (const line of lines) {
    try {
      const report = JSON.parse(line);
      if (report && typeof report === "object") return report;
    } catch {
      // PyInstaller or native dependencies may emit non-JSON diagnostics.
    }
  }
  fail(`command smoke ${args[0]} did not emit a JSON report`);
}

async function smokeTest(bin, port) {
  let startupTimeoutMs;
  try {
    startupTimeoutMs = resolveStartupTimeoutMs(env.VERIFY_BUNDLE_STARTUP_TIMEOUT_MS);
  } catch (error) {
    fail(error.message);
  }

  console.log(
    `[verify-bundle] smoke: launching ${bin} with ${startupTimeoutMs}ms startup budget`,
  );

  try {
    await runBackendSmoke({
      launch: (dataDir) => {
        console.log(
          `[verify-bundle] smoke: ${bin} --port ${port} --data-dir ${dataDir}`,
        );
        return spawn(bin, ["--port", String(port), "--data-dir", dataDir], {
          stdio: ["ignore", "pipe", "pipe"],
        });
      },
      url: `http://127.0.0.1:${port}/m`,
      // Remote/mobile shells are intentionally unavailable in the v1.0 scope.
      // A fully started backend must reject an anonymous /m request; accepting
      // 200 here would be a security regression. The static m.html and chunks
      // are verified independently above.
      isReadyResponse: (response) => response.status === 401,
      startupTimeoutMs,
    });
  } catch (error) {
    const message = error instanceof BackendSmokeError ? error.message : String(error);
    console.error(`\n[verify-bundle] smoke FAILED: ${message}`);
    if (error?.cleanupError) {
      console.error(`[verify-bundle] cleanup FAILED: ${error.cleanupError.message}`);
    }
    console.error("--- last backend output ---");
    console.error((error?.logs ?? "").slice(-4000));
    exit(1);
  }

  console.log(
    "[verify-bundle] smoke: backend is live and anonymous /m failed closed with 401",
  );
}

function fail(msg) {
  console.error(`[verify-bundle] ${msg}`);
  exit(1);
}

function canonicalTextSha256(path) {
  const canonicalText = readFileSync(path, "utf8").replace(/\r\n?/g, "\n");
  return createHash("sha256").update(canonicalText, "utf8").digest("hex");
}

function findEntryByName(directory, pattern) {
  if (!existsSync(directory)) return null;
  for (const entry of readdirSync(directory, { withFileTypes: true })) {
    const path = join(directory, entry.name);
    if (pattern.test(entry.name)) return path;
    if (entry.isDirectory() && !entry.isSymbolicLink()) {
      const nested = findEntryByName(path, pattern);
      if (nested) return nested;
    }
  }
  return null;
}
