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
  readdirSync,
  renameSync,
  rmSync,
  statSync,
  writeFileSync,
} from "node:fs";
import { dirname, join, resolve } from "node:path";
import { tmpdir } from "node:os";
import { argv, env, exit, platform } from "node:process";
import { spawn, spawnSync } from "node:child_process";
import {
  BackendSmokeError,
  resolveStartupTimeoutMs,
  runBackendSmoke,
} from "./verify-bundle-smoke.mjs";
import {
  resolveCheckoutCommit,
  validateOfficeContractReport,
} from "./office-contract-evidence.mjs";

const distArg = argv[2] ?? "backend/dist/suxiaoyou-backend";
const dist = resolve(distArg);

if (!existsSync(dist)) {
  fail(`bundle directory does not exist: ${dist}`);
}

const internal = join(dist, "_internal");
const exeName = platform === "win32" ? "suxiaoyou-backend.exe" : "suxiaoyou-backend";

/**
 * Each entry describes one required asset. `kind` is "file" | "dir"
 * | "nonempty-dir". Add new entries as new resources are bundled —
 * CI will start failing until reality matches the contract.
 */
const required = [
  { kind: "file", path: join(dist, exeName), why: "backend launcher" },

  { kind: "dir", path: internal, why: "PyInstaller _internal/ tree" },

  // Alembic (DB migrations run at startup)
  { kind: "nonempty-dir", path: join(internal, "alembic"), why: "DB migrations" },
  { kind: "file", path: join(internal, "alembic.ini"), why: "alembic config" },

  // Agent prompt templates
  {
    kind: "nonempty-dir",
    path: join(internal, "app", "agent", "prompts"),
    why: "agent prompt templates",
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

if (env.VERIFY_BUNDLE_SKIP_SMOKE === "1") {
  console.log("[verify-bundle] smoke test skipped (VERIFY_BUNDLE_SKIP_SMOKE=1)");
  exit(0);
}

const binary = join(dist, exeName);
const port = 17000 + Math.floor(Math.random() * 500);

providerSmokeTest(binary);
officeSmokeTest(binary);
sandboxSmokeTest(binary);
await smokeTest(binary, port);

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

  const outputPath = String(env.VERIFY_BUNDLE_OFFICE_REPORT ?? "").trim();
  if (outputPath) {
    const destination = resolve(outputPath);
    mkdirSync(dirname(destination), { recursive: true });
    const temporary = `${destination}.tmp-${process.pid}`;
    writeFileSync(temporary, `${JSON.stringify(report, null, 2)}\n`, {
      encoding: "utf8",
      mode: 0o600,
    });
    renameSync(temporary, destination);
    console.log(`[verify-bundle] Office evidence: ${destination}`);
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
