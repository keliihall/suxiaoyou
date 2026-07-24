import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const workflow = readFileSync(join(root, ".github/workflows/release.yml"), "utf8");
const ciWorkflow = readFileSync(join(root, ".github/workflows/ci.yml"), "utf8");
const macSignScript = readFileSync(join(root, "scripts/sign-macos-app.sh"), "utf8");
const tauriConfig = JSON.parse(
  readFileSync(join(root, "desktop-tauri/src-tauri/tauri.conf.json"), "utf8"),
);
const macPlatformConfig = JSON.parse(
  readFileSync(join(root, "desktop-tauri/src-tauri/tauri.macos.conf.json"), "utf8"),
);
const macArmConfig = JSON.parse(
  readFileSync(join(root, "desktop-tauri/src-tauri/build.macos-aarch64.json"), "utf8"),
);
const macIntelConfig = JSON.parse(
  readFileSync(join(root, "desktop-tauri/src-tauri/build.macos-x64.json"), "utf8"),
);
const linuxConfig = JSON.parse(
  readFileSync(join(root, "desktop-tauri/src-tauri/build.linux-x64.json"), "utf8"),
);
const linuxArmConfig = JSON.parse(
  readFileSync(join(root, "desktop-tauri/src-tauri/build.linux-arm64.json"), "utf8"),
);
const installerHooks = readFileSync(
  join(root, "desktop-tauri/src-tauri/installer-hooks.nsh"),
  "utf8",
);
const nsisTemplate = readFileSync(
  join(root, "desktop-tauri/src-tauri/windows/installer.nsi"),
  "utf8",
);
const nsisEnglish = readFileSync(
  join(root, "desktop-tauri/src-tauri/windows/English.nsh"),
  "utf8",
);
const linuxDesktopTemplate = readFileSync(
  join(root, "desktop-tauri/src-tauri/linux/suxiaoyou.desktop.hbs"),
  "utf8",
);
const macEnglishInfoPlist = readFileSync(
  join(root, "desktop-tauri/src-tauri/macos-locales/en.lproj/InfoPlist.strings"),
  "utf8",
);
const macChineseInfoPlist = readFileSync(
  join(root, "desktop-tauri/src-tauri/macos-locales/zh-Hans.lproj/InfoPlist.strings"),
  "utf8",
);
const desktopCargo = readFileSync(
  join(root, "desktop-tauri/src-tauri/Cargo.toml"),
  "utf8",
);
const backendRequirements = readFileSync(
  join(root, "backend/requirements.txt"),
  "utf8",
);
const backendProject = readFileSync(join(root, "backend/pyproject.toml"), "utf8");
const backendSpec = readFileSync(join(root, "backend/suxiaoyou.spec"), "utf8");
const backendEntrypoint = readFileSync(join(root, "backend/run.py"), "utf8");
const bundleVerifier = readFileSync(join(root, "scripts/verify-bundle.mjs"), "utf8");
const officeRendererStager = readFileSync(
  join(root, "scripts/stage-office-renderer.mjs"),
  "utf8",
);
const officeRendererPackaging = readFileSync(
  join(root, "backend/release_packaging/office_renderer_stage.py"),
  "utf8",
);
const backendAdHocEntitlements = readFileSync(
  join(root, "desktop-tauri/src-tauri/entitlements.backend-adhoc.plist"),
  "utf8",
);
const nodeEntitlements = readFileSync(
  join(root, "desktop-tauri/src-tauri/entitlements.node.plist"),
  "utf8",
);

function escapeRegExp(value) {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function job(name) {
  const heading = `  ${name}:`;
  const start = workflow.indexOf(`${heading}\n`);
  assert.notEqual(start, -1, `missing ${name} job`);
  const rest = workflow.slice(start + heading.length + 1);
  const next = rest.search(/^  [a-zA-Z0-9_-]+:\s*$/m);
  return next === -1 ? rest : rest.slice(0, next);
}

function ciJob(name) {
  const heading = `  ${name}:`;
  const start = ciWorkflow.indexOf(`${heading}\n`);
  assert.notEqual(start, -1, `missing CI ${name} job`);
  const rest = ciWorkflow.slice(start + heading.length + 1);
  const next = rest.search(/^  [a-zA-Z0-9_-]+:\s*$/m);
  return next === -1 ? rest : rest.slice(0, next);
}

function step(jobText, name) {
  const heading = `      - name: ${name}`;
  const start = jobText.indexOf(`${heading}\n`);
  assert.notEqual(start, -1, `missing step: ${name}`);
  const rest = jobText.slice(start + heading.length + 1);
  const next = rest.search(/^      - (?:name:|uses:)/m);
  return next === -1 ? rest : rest.slice(0, next);
}

test("supports tag releases and explicitly marked manual test builds", () => {
  assert.match(workflow, /^\s{2}push:\s*$/m);
  assert.match(workflow, /^\s{4}tags:\s*\["v\*"\]\s*$/m);
  assert.match(workflow, /^\s{2}workflow_dispatch:\s*$/m);

  const publish = job("publish");
  assert.match(publish, /github\.event_name == 'push'/);
  assert.match(publish, /startsWith\(github\.ref, 'refs\/tags\/'\)/);
  assert.match(
    publish,
    /needs\.validate-release\.outputs\.evidence_contract == 'v1\.0'/,
  );
  assert.match(
    publish,
    /needs\.validate-release\.outputs\.release_profile == 'unsigned-degraded'/,
  );

  const mac = job("build-macos");
  assert.match(mac, /--no-sign/);
  assert.match(mac, /MACOS_ARTIFACT_PROFILE/);

  const validate = job("validate-release");
  assert.match(validate, /fetch-depth:\s*0/);
  assert.match(validate, /Require official tag commit on main/);
  assert.match(validate, /git merge-base --is-ancestor "\$GITHUB_SHA" refs\/remotes\/origin\/main/);
  assert.match(validate, /Resolve and validate release context/);
  assert.match(validate, /RELEASE_CHANNEL="manual-test"/);
  assert.match(validate, /MACOS_ARTIFACT_PROFILE="ADHOC-TEST"/);
});

test("preflights target-aware v1.1 renderer configuration before dependencies and downloads", () => {
  const validate = job("validate-release");
  const preflightName = "Preflight v1.1 Office renderer CI configuration";
  const preflight = step(validate, preflightName);

  const orderedSteps = [
    "Resolve and validate release context",
    preflightName,
    "Install validation dependencies",
  ].map((name) => validate.indexOf(`      - name: ${name}`));
  assert.ok(orderedSteps.every((index) => index >= 0));
  assert.deepEqual(
    orderedSteps,
    [...orderedSteps].sort((left, right) => left - right),
    "renderer configuration must be selected after version resolution and fail before dependency installation",
  );
  assert.match(
    preflight,
    /if:\s*startsWith\(steps\.release-context\.outputs\.app_version, '1\.1\.'\)/,
  );
  assert.match(
    preflight,
    /SUXIAOYOU_OFFICE_RENDERER_ARTIFACT_REPOSITORY:\s*\$\{\{ vars\.SUXIAOYOU_OFFICE_RENDERER_ARTIFACT_REPOSITORY \}\}/,
  );
  assert.match(
    preflight,
    /SUXIAOYOU_OFFICE_RENDERER_ARTIFACT_RUN_ID:\s*\$\{\{ vars\.SUXIAOYOU_OFFICE_RENDERER_ARTIFACT_RUN_ID \}\}/,
  );
  assert.match(
    preflight,
    /SUXIAOYOU_OFFICE_RENDERER_ARTIFACT_TOKEN:\s*\$\{\{ secrets\.SUXIAOYOU_OFFICE_RENDERER_ARTIFACT_TOKEN \}\}/,
  );
  assert.match(
    preflight,
    /ARTIFACT_REPOSITORY" =~ \^\[A-Za-z0-9\]/,
  );
  assert.match(preflight, /ARTIFACT_RUN_ID" =~ \^\[1-9\]\[0-9\]\*\$/);
  assert.match(
    preflight,
    /! "\$SUXIAOYOU_OFFICE_RENDERER_ARTIFACT_TOKEN" =~ \[\^\[:space:\]\]/,
  );
  assert.match(preflight, /\^\[0-9a-f\]\{64\}\$/);
  assert.match(preflight, /"\$value" == "\$zero_sha256"/);

  const targetLocks = new Map([
    ["windows-x64", "SUXIAOYOU_OFFICE_RENDERER_LOCK_SHA256_WINDOWS_X64"],
    ["windows-arm64", "SUXIAOYOU_OFFICE_RENDERER_LOCK_SHA256_WINDOWS_ARM64"],
    ["macos-aarch64", "SUXIAOYOU_OFFICE_RENDERER_LOCK_SHA256_DARWIN_ARM64"],
    ["macos-x64", "SUXIAOYOU_OFFICE_RENDERER_LOCK_SHA256_DARWIN_X64"],
    ["linux-arm64", "SUXIAOYOU_OFFICE_RENDERER_LOCK_SHA256_LINUX_ARM64"],
    ["linux-x64", "SUXIAOYOU_OFFICE_RENDERER_LOCK_SHA256_LINUX_X64"],
  ]);
  for (const [target, lockName] of targetLocks) {
    assert.match(
      preflight,
      new RegExp(
        `${escapeRegExp(target)}\\) required_lock_names=\\("${lockName}"\\) ;;`,
      ),
      `${target} must select only ${lockName} for workflow_dispatch`,
    );
    assert.match(
      preflight,
      new RegExp(
        `${lockName}:\\s*\\$\\{\\{ vars\\.${lockName} \\}\\}`,
      ),
    );
  }

  const tagLockBlock = preflight.match(
    /if \[\[ "\$GITHUB_EVENT_NAME" == "push"[^]*?required_lock_names=\(([^]*?)\n\s*\)/,
  );
  assert.ok(tagLockBlock, "tag releases must declare their complete renderer lock set");
  for (const lockName of targetLocks.values()) {
    assert.match(tagLockBlock[1], new RegExp(`"${lockName}"`));
  }
  assert.match(preflight, /\*\) invalid\+=\("workflow_dispatch\.inputs\.target"\)/);
  assert.match(
    preflight,
    /echo "::error::invalid v1\.1 Office renderer CI configuration: \$\{invalid\[\*\]\}"/,
  );
  assert.doesNotMatch(preflight, /echo[^\n]*\$(?:value|SUXIAOYOU_OFFICE_RENDERER)/);

  for (const jobName of ["build-windows", "build-macos", "build-linux"]) {
    const build = job(jobName);
    assert.match(build, /needs:\s*validate-release/);
    assert.ok(
      build.indexOf("Materialize authenticated Office renderer source for v1.1") <
        build.indexOf("Stage immutable Office renderer for v1.1"),
      `${jobName} must not stage before the authenticated download`,
    );
  }
});

test("renderer configuration preflight executes target selection without leaking values", () => {
  const preflight = step(
    job("validate-release"),
    "Preflight v1.1 Office renderer CI configuration",
  );
  const runMarker = "        run: |\n";
  const runOffset = preflight.indexOf(runMarker);
  assert.notEqual(runOffset, -1);
  const script = preflight
    .slice(runOffset + runMarker.length)
    .split("\n")
    .map((line) => line.replace(/^ {10}/u, ""))
    .join("\n");
  const lockNames = [
    "SUXIAOYOU_OFFICE_RENDERER_LOCK_SHA256_WINDOWS_X64",
    "SUXIAOYOU_OFFICE_RENDERER_LOCK_SHA256_WINDOWS_ARM64",
    "SUXIAOYOU_OFFICE_RENDERER_LOCK_SHA256_DARWIN_ARM64",
    "SUXIAOYOU_OFFICE_RENDERER_LOCK_SHA256_DARWIN_X64",
    "SUXIAOYOU_OFFICE_RENDERER_LOCK_SHA256_LINUX_ARM64",
    "SUXIAOYOU_OFFICE_RENDERER_LOCK_SHA256_LINUX_X64",
  ];
  const baseEnvironment = {
    ...process.env,
    SUXIAOYOU_OFFICE_RENDERER_ARTIFACT_REPOSITORY: "owner/private-renderer",
    SUXIAOYOU_OFFICE_RENDERER_ARTIFACT_RUN_ID: "12345",
    SUXIAOYOU_OFFICE_RENDERER_ARTIFACT_TOKEN: "renderer-secret-value",
    GITHUB_REF: "refs/heads/main",
  };
  for (const name of lockNames) baseEnvironment[name] = "";
  const run = (environment) =>
    spawnSync("bash", ["-c", script], {
      encoding: "utf8",
      env: { ...baseEnvironment, ...environment },
    });

  const manual = run({
    GITHUB_EVENT_NAME: "workflow_dispatch",
    RELEASE_TARGET: "windows-x64",
    [lockNames[0]]: "a".repeat(64),
  });
  assert.equal(manual.status, 0, manual.stderr);

  const unknown = run({
    GITHUB_EVENT_NAME: "workflow_dispatch",
    RELEASE_TARGET: "unknown",
  });
  assert.equal(unknown.status, 1);
  assert.match(unknown.stdout, /workflow_dispatch\.inputs\.target/u);
  assert.doesNotMatch(unknown.stderr, /unbound variable/u);

  const tagLocks = Object.fromEntries(
    lockNames.map((name) => [name, "b".repeat(64)]),
  );
  const incompleteTag = run({
    ...tagLocks,
    GITHUB_EVENT_NAME: "push",
    GITHUB_REF: "refs/tags/v1.1.0",
    RELEASE_TARGET: "",
    [lockNames.at(-1)]: "",
  });
  assert.equal(incompleteTag.status, 1);
  assert.match(incompleteTag.stdout, new RegExp(lockNames.at(-1), "u"));

  const blankToken = run({
    GITHUB_EVENT_NAME: "workflow_dispatch",
    RELEASE_TARGET: "windows-x64",
    SUXIAOYOU_OFFICE_RENDERER_ARTIFACT_TOKEN: " \t ",
    [lockNames[0]]: "c".repeat(64),
  });
  assert.equal(blankToken.status, 1);
  assert.match(
    blankToken.stdout,
    /SUXIAOYOU_OFFICE_RENDERER_ARTIFACT_TOKEN/u,
  );
  for (const output of [unknown.stdout, incompleteTag.stdout, blankToken.stdout]) {
    assert.doesNotMatch(output, /renderer-secret-value|owner\/private-renderer/u);
  }
});

test("manual releases select one target while tags keep both native macOS builds", () => {
  const mac = job("build-macos");
  for (const target of [
    "macos-aarch64",
    "macos-x64",
    "windows-x64",
    "windows-arm64",
    "linux-x64",
    "linux-arm64",
  ]) {
    assert.match(workflow, new RegExp(`^\\s{10}- ${escapeRegExp(target)}$`, "m"));
  }
  assert.match(workflow, /^\s{8}default:\s*macos-aarch64$/m);
  const windows = job("build-windows");
  assert.match(windows, /needs\.validate-release\.outputs\.run_windows == 'true'/);
  assert.match(windows, /fromJSON\(needs\.validate-release\.outputs\.windows_matrix\)/);
  assert.match(windows, /runs-on:\s*\$\{\{ matrix\.runner \}\}/);
  assert.match(windows, /architecture:\s*\$\{\{ matrix\.pythonArchitecture \}\}/);
  const validate = job("validate-release");
  assert.match(validate, /"target":"windows-x64"[^]*?"runner":"windows-latest"/);
  assert.match(validate, /"target":"windows-arm64"[^]*?"runner":"windows-11-arm"/);
  const linux = job("build-linux");
  assert.match(linux, /startsWith\(inputs\.target, 'linux-'\)/);
  assert.match(linux, /fromJSON\([^\n]*linux-x64[^\n]*linux-arm64/);
  assert.match(linux, /runs-on:[^\n]*ubuntu-22\.04-arm[^\n]*ubuntu-22\.04/);
  assert.match(linux, /BUILD_CONFIG:[^\n]*build\.linux-arm64\.json[^\n]*build\.linux-x64\.json/);
  assert.match(linux, /EXPECTED_DEB_ARCH:[^\n]*arm64[^\n]*amd64/);
  assert.match(linux, /EXPECTED_RPM_ARCH:[^\n]*aarch64[^\n]*x86_64/);
  assert.match(mac, /inputs\.target == 'macos-aarch64'/);
  assert.match(mac, /inputs\.target == 'macos-x64'/);
  assert.match(mac, /fromJSON\([^\n]*aarch64-apple-darwin[^\n]*x86_64-apple-darwin/);
  assert.match(mac, /runs-on:[^\n]*macos-14[^\n]*macos-15-intel/);
  assert.match(mac, /BUNDLE_ARCH:[^\n]*arm64[^\n]*x86_64/);
  assert.match(mac, /DMG_ARCH:[^\n]*aarch64[^\n]*x64/);
  assert.match(mac, /NODE_PLATFORM:[^\n]*Darwin-arm64[^\n]*Darwin-x86_64/);
  assert.match(mac, /BACKEND_DIST:\s*dist/);
  assert.match(mac, /BUILD_CONFIG:[^\n]*build\.macos-aarch64\.json[^\n]*build\.macos-x64\.json/);
  assert.match(mac, /backend\/\$BACKEND_DIST\/suxiaoyou-backend/);
  assert.match(mac, /--config "\$BUILD_CONFIG"/);
  assert.match(mac, /node scripts\/verify-macos-bundle\.mjs/);
  assert.doesNotMatch(mac, /VERIFY_BUNDLE_SKIP_SMOKE/);
  assert.doesNotMatch(mac, /dist-x86_64/);
  assert.deepEqual(macPlatformConfig.bundle.targets, ["dmg", "app"]);
  assert.equal(
    macPlatformConfig.bundle.resources["macos-locales/en.lproj/InfoPlist.strings"],
    "en.lproj/InfoPlist.strings",
  );
  assert.equal(
    macPlatformConfig.bundle.resources["macos-locales/zh-Hans.lproj/InfoPlist.strings"],
    "zh-Hans.lproj/InfoPlist.strings",
  );
  for (const config of [macArmConfig, macIntelConfig]) {
    for (const [source, destination] of Object.entries(tauriConfig.bundle.resources)) {
      assert.equal(config.bundle.resources[source], destination);
    }
    assert.equal(
      Object.keys(config.bundle.resources).some((path) => path.includes("macos-locales")),
      false,
    );
  }
  assert.match(macEnglishInfoPlist, /"CFBundleDisplayName" = "suyo";/);
  assert.match(macChineseInfoPlist, /"CFBundleDisplayName" = "苏小有";/);
  assert.ok(
    Object.hasOwn(
      macArmConfig.bundle.resources,
      "../../backend/dist/suxiaoyou-backend",
    ),
  );
  assert.ok(
    Object.hasOwn(
      macIntelConfig.bundle.resources,
      "../../backend/dist/suxiaoyou-backend",
    ),
  );
  assert.deepEqual(
    Object.keys(macIntelConfig.bundle.resources).filter((path) =>
      path.includes("suxiaoyou-backend"),
    ),
    ["../../backend/dist/suxiaoyou-backend"],
  );
});

test("installer artifacts expire after one day and lifecycle evidence spans the Beta window", () => {
  const jobs = [
    [
      "build-windows",
      "Upload Windows artifact",
      "Upload Windows lifecycle diagnostics",
      "suxiaoyou-desktop-lifecycle-${{ matrix.target }}",
    ],
    [
      "build-macos",
      "Upload macOS artifact",
      "Upload macOS lifecycle diagnostics",
      "suxiaoyou-desktop-lifecycle-macos-${{ env.DMG_ARCH }}",
    ],
    [
      "build-linux",
      "Upload Linux artifacts",
      "Upload Linux lifecycle diagnostics",
      "suxiaoyou-desktop-lifecycle-linux-*",
    ],
  ];
  const diagnosticPaths = new Set();
  for (const [name, installerStep, diagnosticsStep, diagnosticPath] of jobs) {
    const build = job(name);
    assert.equal((build.match(/uses: actions\/upload-artifact@/g) ?? []).length, 2);
    assert.match(step(build, installerStep), /if:\s*success\(\)/);
    assert.match(step(build, installerStep), /overwrite:\s*true/);
    assert.match(step(build, installerStep), /retention-days:\s*1/);
    const diagnostics = step(build, diagnosticsStep);
    assert.match(diagnostics, /if:\s*always\(\)/);
    assert.doesNotMatch(diagnostics, /overwrite:/);
    assert.match(diagnostics, /if-no-files-found:\s*ignore/);
    assert.match(diagnostics, /retention-days:\s*30/);
    assert.match(diagnostics, new RegExp(escapeRegExp(diagnosticPath)));
    assert.equal(diagnosticPaths.has(diagnosticPath), false, diagnosticPath);
    diagnosticPaths.add(diagnosticPath);
  }
  const windowsDiagnostics = step(
    job("build-windows"),
    "Upload Windows lifecycle diagnostics",
  );
  assert.match(
    windowsDiagnostics,
    /suxiaoyou-windows-arm64-wheelhouse\/windows-arm64-wheelhouse-manifest\.json/,
  );
  assert.match(
    windowsDiagnostics,
    /suxiaoyou-windows-arm64-wheelhouse\/windows-arm64-wheelhouse-manifest\.json\.sha256/,
  );
});

test("sets the declared macOS minimum everywhere", () => {
  assert.equal(tauriConfig.bundle.macOS.minimumSystemVersion, "11.0");
  assert.match(job("build-macos"), /MACOSX_DEPLOYMENT_TARGET:\s*"11\.0"/);
});

test("preserves package upgrade identities while localizing visible display names", () => {
  assert.equal(tauriConfig.productName, "苏小有");
  assert.ok(tauriConfig.app.windows.every((window) => window.title === "苏小有"));
  assert.equal(linuxConfig.productName, "suxiaoyou");
  assert.equal(linuxArmConfig.productName, "suxiaoyou");
  assert.equal(linuxConfig.mainBinaryName, undefined);
  assert.equal(linuxArmConfig.mainBinaryName, undefined);
  assert.deepEqual(linuxConfig.bundle.resources, tauriConfig.bundle.resources);
  assert.deepEqual(linuxArmConfig.bundle.resources, tauriConfig.bundle.resources);

  const templatePath = "linux/suxiaoyou.desktop.hbs";
  assert.equal(linuxConfig.bundle.linux.deb.desktopTemplate, templatePath);
  assert.equal(linuxConfig.bundle.linux.rpm.desktopTemplate, templatePath);
  assert.equal(linuxArmConfig.bundle.linux.deb.desktopTemplate, templatePath);
  assert.equal(linuxArmConfig.bundle.linux.rpm.desktopTemplate, templatePath);
  assert.deepEqual(linuxConfig.bundle.linux.deb.depends, ["bubblewrap", "libxdo3"]);
  assert.deepEqual(linuxConfig.bundle.linux.rpm.depends, ["bubblewrap", "xdotool"]);
  assert.deepEqual(linuxArmConfig.bundle.linux.deb.depends, ["bubblewrap", "libxdo3"]);
  assert.deepEqual(linuxArmConfig.bundle.linux.rpm.depends, ["bubblewrap", "xdotool"]);
  assert.match(linuxDesktopTemplate, /^\[Desktop Entry\]$/m);
  assert.match(linuxDesktopTemplate, /^Categories=\{\{categories\}\}$/m);
  assert.match(linuxDesktopTemplate, /^Comment=\{\{comment\}\}$/m);
  assert.match(linuxDesktopTemplate, /^Exec=\{\{exec\}\}$/m);
  assert.match(linuxDesktopTemplate, /^StartupWMClass=\{\{exec\}\}$/m);
  assert.match(linuxDesktopTemplate, /^Icon=\{\{icon\}\}$/m);
  assert.match(linuxDesktopTemplate, /^Name=suyo$/m);
  assert.match(linuxDesktopTemplate, /^Name\[zh_CN\]=苏小有$/m);
  assert.match(linuxDesktopTemplate, /^Terminal=false$/m);
  assert.match(linuxDesktopTemplate, /^Type=Application$/m);
  assert.match(linuxDesktopTemplate, /^MimeType=\{\{mime_type\}\}$/m);
  assert.doesNotMatch(linuxDesktopTemplate, /^Name=\{\{name\}\}$/m);
});

test("preserves stable NSIS identity while localizing visible installer copy", () => {
  const nsis = tauriConfig.bundle.windows.nsis;
  assert.deepEqual(nsis.languages, ["English", "SimpChinese"]);
  assert.equal(nsis.displayLanguageSelector, true);
  assert.equal(nsis.template, "windows/installer.nsi");
  assert.deepEqual(nsis.customLanguageFiles, {
    English: "windows/English.nsh",
  });
  assert.match(installerHooks, /LangString SuyoClosingProcesses \$\{LANG_ENGLISH\} "Closing running suyo background processes\.\.\."/);
  assert.match(installerHooks, /LangString SuyoClosingProcesses \$\{LANG_SIMPCHINESE\} "正在关闭运行中的苏小有后台进程\.\.\."/);
  assert.match(installerHooks, /!macro NSIS_HOOK_LANGSTRINGS/);
  assert.match(installerHooks, /DetailPrint "\$\(SuyoClosingProcesses\)"/);
  assert.match(
    nsisTemplate,
    /Vendored from tauri-bundler 2\.9\.4's stock NSIS installer template/,
  );
  assert.match(
    nsisTemplate,
    /Upstream SHA-256: 20f4ecc730defb71f1342eaeaec4021df13be3d843abba0effe88ea5835fa079/,
  );
  assert.ok(nsisTemplate.includes('!define PRODUCTNAME "{{product_name}}"'));
  assert.ok(
    nsisTemplate.includes(
      '!define UNINSTKEY "Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\${PRODUCTNAME}"',
    ),
  );
  assert.ok(nsisTemplate.includes('!define MANUPRODUCTKEY "${MANUKEY}\\${PRODUCTNAME}"'));
  assert.ok(nsisTemplate.includes('!define PLACEHOLDER_INSTALL_DIR "placeholder\\${PRODUCTNAME}"'));
  assert.ok(nsisTemplate.includes('StrCpy $INSTDIR "$LOCALAPPDATA\\${PRODUCTNAME}"'));
  assert.match(nsisTemplate, /LangString SuyoDisplayName \$\{LANG_ENGLISH\} "suyo"/);
  assert.match(nsisTemplate, /LangString SuyoDisplayName \$\{LANG_SIMPCHINESE\} "苏小有"/);
  const muiLanguageLoad = '!insertmacro MUI_LANGUAGE "{{this}}"';
  const displayNameLangString =
    'LangString SuyoDisplayName ${LANG_ENGLISH} "suyo"';
  assert.ok(
    nsisTemplate.indexOf(muiLanguageLoad) < nsisTemplate.indexOf(displayNameLangString),
    "custom LangStrings must be declared only after MUI has defined the language IDs",
  );
  assert.ok(
    nsisTemplate.indexOf(displayNameLangString)
      < nsisTemplate.indexOf("!insertmacro NSIS_HOOK_LANGSTRINGS"),
  );
  assert.ok(nsisTemplate.includes('Name "$(SuyoDisplayName)"'));
  assert.ok(nsisTemplate.includes('VIAddVersionKey "ProductName" "suyo"'));
  assert.ok(nsisTemplate.includes('VIAddVersionKey "FileDescription" "suyo"'));
  assert.ok(
    nsisTemplate.includes(
      'CheckIfAppIsRunning "${MAINBINARYNAME}.exe" "$SuyoLocalizedName"',
    ),
  );
  assert.ok(
    nsisTemplate.includes(
      'CheckIfAppIsRunning "${MAINBINARYNAME}.exe" "$(SuyoDisplayName)"',
    ),
  );
  assert.ok(nsisTemplate.includes('"Open with $SuyoLocalizedName"'));
  assert.ok(
    nsisTemplate.includes(
      'WriteRegStr SHCTX "${UNINSTKEY}" "DisplayName" "$SuyoLocalizedName"',
    ),
  );
  assert.equal(
    nsisTemplate.match(
      /WriteRegStr SHCTX "\$\{UNINSTKEY\}" "DisplayName"/g,
    )?.length,
    1,
  );
  assert.ok(
    nsisTemplate.includes(
      'CreateShortcut "$SMPROGRAMS\\$SuyoLocalizedShortcutName" "$INSTDIR\\${MAINBINARYNAME}.exe"',
    ),
  );
  assert.ok(
    nsisTemplate.includes(
      'CreateShortcut "$DESKTOP\\$SuyoLocalizedShortcutName" "$INSTDIR\\${MAINBINARYNAME}.exe"',
    ),
  );
  assert.doesNotMatch(nsisTemplate, /CreateShortcut "[^"\n]*\$\{PRODUCTNAME\}\.lnk"/);
  for (const shortcutPath of [
    "$SMPROGRAMS\\${PRODUCTNAME}.lnk",
    "$SMPROGRAMS\\suyo.lnk",
    "$DESKTOP\\${PRODUCTNAME}.lnk",
    "$DESKTOP\\suyo.lnk",
  ]) {
    assert.ok(
      nsisTemplate.includes(
        `SuyoDeleteShortcutIfTarget "${shortcutPath}" "$INSTDIR\\\${MAINBINARYNAME}.exe"`,
      ),
      shortcutPath,
    );
  }
  assert.ok(nsisTemplate.includes("Call MigrateOwnedDesktopShortcut"));
  assert.doesNotMatch(nsisTemplate, /^Name "\$\{PRODUCTNAME\}"$/m);
  assert.doesNotMatch(nsisEnglish, /[\u3400-\u9fff]/u);
  assert.doesNotMatch(nsisEnglish, /\$\{PRODUCTNAME\}/);
  assert.equal(nsisEnglish.match(/\{\{product_name\}\}/g)?.length, 3);
  assert.match(nsisEnglish, /suyo \$\{VERSION\} is already installed/);
  assert.match(nsisEnglish, /Choose how you want to install suyo\./);
  assert.match(nsisEnglish, /Uninstall suyo/);
  assert.match(
    workflow,
    /Windows NSIS：安装界面支持 English 与简体中文并提供语言选择；当前未配置 Authenticode/,
  );
  assert.match(
    workflow,
    /八个安装包（Windows x64\/ARM64、macOS arm64\/x64、Linux x64\/ARM64）均来自同一提交/,
  );
});

test("keeps Apple credentials step-scoped and fails stable tags fast", () => {
  const beforeJobs = workflow.slice(0, workflow.indexOf("\njobs:\n"));
  assert.doesNotMatch(beforeJobs, /APPLE_(?:CERTIFICATE|CERTIFICATE_PASSWORD|ID|PASSWORD|TEAM_ID)/);

  for (const line of workflow.split("\n")) {
    if (!line.includes("secrets.APPLE_")) continue;
    const indentation = line.match(/^\s*/)[0].length;
    assert.ok(indentation >= 10, `Apple secret is not step-scoped: ${line.trim()}`);
  }

  const validate = job("validate-release");
  const credentials = step(validate, "Require Apple credentials for a stable tag");
  assert.match(credentials, /if:\s*github\.event_name == 'push'/);
  assert.match(credentials, /steps\.release-context\.outputs\.is_stable == 'true'/);
  for (const name of [
    "APPLE_CERTIFICATE",
    "APPLE_CERTIFICATE_PASSWORD",
    "APPLE_ID",
    "APPLE_PASSWORD",
    "APPLE_TEAM_ID",
  ]) {
    assert.match(credentials, new RegExp(`${name}:\\s*\\$\\{\\{ secrets\\.${name} \\}\\}`));
    assert.match(credentials, new RegExp(`\\$${name}(?:[^A-Z_]|$)`));
  }
  assert.match(credentials, /missing/);
  assert.match(credentials, /exit 1/);
});

test("separates app version from stable and RC release identities", () => {
  const validate = job("validate-release");
  const context = step(validate, "Resolve and validate release context");
  assert.match(context, /APP_VERSION=.*package\.json/);
  assert.match(context, /GITHUB_REF_NAME.*v\$APP_VERSION/);
  assert.match(context, /-rc\\\.\(\[1-9\]\[0-9\]\*\)/);
  assert.match(context, /RELEASE_VERSION="\$\{GITHUB_REF_NAME#v\}"/);
  assert.match(context, /RELEASE_CHANNEL="stable"/);
  assert.match(context, /RELEASE_CHANNEL="prerelease"/);
  assert.match(context, /IS_STABLE="true"/);
  assert.match(context, /IS_STABLE="false"/);
  assert.match(context, /RC-ADHOC-NOT-NOTARIZED/);
  assert.match(context, /tag must be v\$APP_VERSION or v\$APP_VERSION-rc\.N/);
  assert.match(context, /1\.0\.0\)[\s\S]*?EVIDENCE_CONTRACT="v1\.0"[\s\S]*?RELEASE_PROFILE="authoritative"/);
  assert.match(context, /1\.1\.\*\)[\s\S]*?EVIDENCE_CONTRACT="v1\.1"[\s\S]*?RELEASE_PROFILE="unsigned-degraded"/);
  assert.match(context, /tagged releases require a reviewed evidence contract/);
  assert.match(context, /RELEASE_CHANNEL="stable"[\s\S]*?IS_STABLE="false"[\s\S]*?MACOS_ARTIFACT_PROFILE="UNSIGNED-DEGRADED"/);
  assert.doesNotMatch(context, /unsigned-degraded v1\.1 release uses the exact/);
  assert.match(context, /RELEASE_PROFILE" == "unsigned-degraded"[\s\S]*?MACOS_ARTIFACT_PROFILE="UNSIGNED-DEGRADED"/);
  assert.match(context, /release_profile=\$RELEASE_PROFILE/);
  assert.match(context, /evidence_contract=\$EVIDENCE_CONTRACT/);
  assert.doesNotMatch(validate, /Block v1\.1 tags/);

  const metadata = step(validate, "Validate metadata and workflow contracts");
  assert.match(metadata, /APP_VERSION:\s*\$\{\{ steps\.release-context\.outputs\.app_version \}\}/);
  assert.match(metadata, /release-metadata\.mjs "\$APP_VERSION"/);
  assert.doesNotMatch(metadata, /GITHUB_REF_NAME#v/);
});

test("v1.0 RC captures contracts while stable tags fail closed on both real integrations", () => {
  const validate = job("validate-release");
  const capture = step(validate, "Capture v1 integration contract evidence");
  assert.match(capture, /startsWith\(github\.ref, 'refs\/tags\/'\)/);
  assert.match(capture, /evidence_contract == 'v1\.0'/);
  assert.doesNotMatch(capture, /refs\/tags\/v1\.0\.0/);
  assert.match(capture, /v1-real-integration-gates\.mjs contract/);
  assert.match(capture, /--require-evidence-eligible true/);
  assert.match(capture, /\$RUNNER_TEMP\/v1-integration-evidence/);

  const credentials = step(validate, "Require real integration credentials for stable tag");
  assert.match(credentials, /is_stable == 'true'/);
  assert.match(credentials, /evidence_contract == 'v1\.0'/);
  for (const name of [
    "TENCENT_DOCS_E2E_TOKEN",
    "TENCENT_DOCS_E2E_TEST_DOCUMENT_ID",
    "TENCENT_DOCS_E2E_BASELINE_TEXT",
    "TENCENT_DOCS_E2E_READ_ARGS_JSON",
    "TENCENT_DOCS_E2E_WRITE_TOOL",
    "TENCENT_DOCS_E2E_WRITE_ARGS_JSON",
    "TENCENT_DOCS_E2E_RESTORE_TOOL",
    "TENCENT_DOCS_E2E_RESTORE_ARGS_JSON",
    "SILICONFLOW_IMAGE_E2E_API_KEY",
    "SILICONFLOW_IMAGE_E2E_MAX_COST_CNY",
  ]) {
    assert.match(credentials, new RegExp(`${name}:\\s*\\$\\{\\{ secrets\\.${name} \\}\\}`));
    assert.match(credentials, new RegExp(`\\b${name}\\b`));
  }
  assert.match(credentials, /missing/);
  assert.match(credentials, /exit 1/);

  const live = step(validate, "Capture stable real integration evidence");
  assert.match(live, /is_stable == 'true'/);
  assert.match(live, /evidence_contract == 'v1\.0'/);
  assert.match(live, /I_UNDERSTAND_THIS_MODIFIES_A_DEDICATED_TEST_DOCUMENT/);
  assert.match(live, /I_UNDERSTAND_THIS_MAY_USE_PROVIDER_QUOTA_OR_INCUR_COST/);
  assert.match(live, /SILICONFLOW_IMAGE_E2E_MAX_REQUESTS:\s*"1"/);
  assert.match(live, /live tencent-real-write/);
  assert.match(live, /live siliconflow-image-real/);

  const upload = step(validate, "Upload v1 integration evidence");
  assert.match(upload, /if:\s*always\(\)/);
  assert.match(upload, /evidence_contract == 'v1\.0'/);
  assert.match(upload, /v1-integration-evidence-\$\{\{ github\.run_attempt \}\}/);
  assert.match(upload, /runner\.temp.*v1-integration-evidence/);
  assert.match(upload, /retention-days:\s*30/);
  assert.match(upload, /if-no-files-found:\s*error/);

  const publish = job("publish");
  const verify = step(publish, "Verify and summarize v1 integration evidence");
  assert.match(verify, /if:\s*env\.RELEASE_EVIDENCE_CONTRACT == 'v1\.0'/);
  assert.match(verify, /INTEGRATION_MODE="rc"/);
  assert.match(verify, /INTEGRATION_MODE="ga"/);
  assert.doesNotMatch(verify, /INTEGRATION_MODE="contract"/);
  assert.match(verify, /v1-real-integration-gates\.mjs verify/);
  assert.match(verify, /v1-real-integration-gates\.mjs summarize/);
  assert.match(verify, /--release-tag "\$GITHUB_REF_NAME"/);
  assert.match(verify, /--commit "\$RELEASE_COMMIT"/);
  assert.match(verify, /--output INTEGRATION-CONTRACTS\.json/);
  assert.match(
    publish,
    /RELEASE_CAPABILITIES_ASSET:[^\n]*'DEGRADED-CAPABILITIES\.json'[^\n]*'INTEGRATION-CONTRACTS\.json'/,
  );
  assert.match(
    step(publish, "Prepare GitHub Release"),
    /\$\{\{ env\.RELEASE_CAPABILITIES_ASSET \}\}/,
  );
});

test("publishes a manual-download manifest without updater artifacts or keys", () => {
  assert.doesNotMatch(
    workflow,
    /TAURI_SIGNING_PRIVATE_KEY|\.app\.tar\.gz|\.exe\.sig|latest\.json/,
  );
  assert.match(workflow, /release-manifest\.json/);
  assert.match(workflow, /manual-download/);
  assert.match(workflow, /generate-release-manifest\.mjs/);
  assert.match(workflow, /verify-release-manifest\.mjs/);
  assert.match(workflow, /git rev-parse 'HEAD\^\{commit\}'/);
});

test("uses locked desktop tooling and sanitized frontend output on every platform", () => {
  for (const name of ["build-windows", "build-macos", "build-linux"]) {
    const build = job(name);
    assert.match(build, /npm --prefix desktop-tauri ci/);
    assert.match(build, /npm run build:frontend/);
    assert.doesNotMatch(build, /npx (?:next|@tauri-apps\/cli)/);
    assert.match(build, /npm exec tauri build/);
  }
});

test("pins every native build phase and final upload to the release checkout", () => {
  const builds = [
    ["build-windows", "Build backend with locked PyInstaller", "Build Tauri NSIS installer", "Upload Windows artifact"],
    ["build-macos", "Build native backend with locked PyInstaller", "Build Tauri app for post-copy repair", "Upload macOS artifact"],
    ["build-linux", "Build backend with locked PyInstaller", "Build Tauri Linux installers", "Upload Linux artifacts"],
  ];
  for (const [jobName, backendName, tauriName, uploadName] of builds) {
    const build = job(jobName);
    const initial = step(build, "Verify release checkout before dependency installation");
    const frontend = step(build, "Build and sanitize frontend");
    const backend = step(build, backendName);
    const tauriSeal = step(build, "Seal verified Tauri build inputs");
    const tauri = step(build, tauriName);
    const final = step(build, "Verify release checkout before artifact upload");
    const upload = step(build, uploadName);

    assert.match(initial, /node scripts\/verify-release-checkout\.mjs "\$GITHUB_SHA"/);
    assert.equal(
      (frontend.match(/verify-release-checkout\.mjs "\$GITHUB_SHA"/g) ?? []).length,
      2,
      `${jobName} must verify source before and after frontend compilation`,
    );
    assert.match(frontend, /release-input-seal\.mjs create/);
    assert.match(frontend, /suxiaoyou-frontend-inputs\.json/);
    assert.match(backend, /verify_release_checkout\(\)[\s\S]*verify-release-checkout\.mjs "\$GITHUB_SHA"/);
    assert.equal(
      (backend.match(/release-input-seal\.mjs verify/g) ?? []).length,
      2,
      `${jobName} must preserve the exact frontend tree around PyInstaller`,
    );
    assert.match(tauriSeal, /suxiaoyou-frontend-inputs\.json/);
    assert.match(tauriSeal, /release-input-seal\.mjs create/);
    for (const input of [
      "frontend/out",
      "backend/dist/suxiaoyou-backend",
      "backend/resources/nodejs",
    ]) {
      assert.match(tauriSeal, new RegExp(escapeRegExp(input)));
    }
    assert.equal(
      (tauri.match(/verify-release-checkout\.mjs "\$GITHUB_SHA"/g) ?? []).length,
      2,
      `${jobName} must verify source before and after Tauri compilation`,
    );
    assert.equal(
      (tauri.match(/release-input-seal\.mjs verify/g) ?? []).length,
      2,
      `${jobName} must preserve the exact generated Tauri inputs`,
    );
    assert.match(final, /node scripts\/verify-release-checkout\.mjs "\$GITHUB_SHA"/);
    assert.match(final, /release-input-seal\.mjs verify/);
    assert.match(final, /verify-desktop-lifecycle\.mjs verify-artifact/);
    assert.match(final, /--release-commit "\$GITHUB_SHA"/);
    assert.match(upload, /if:\s*success\(\)/);
    assert.ok(
      build.indexOf("Verify release checkout before artifact upload") <
        build.indexOf(`      - name: ${uploadName}`),
      `${jobName} must perform the final source check before upload`,
    );
  }
});

test("prepares exactly the ignored Tauri resource roots only in validation jobs", () => {
  const expectedResources = [
    "../../backend/dist/suxiaoyou-backend",
    "../../backend/resources/nodejs",
  ];
  const configuredBackendResources = Object.keys(
    tauriConfig.bundle.resources,
  )
    .filter((path) => path.startsWith("../../backend/"))
    .sort();
  assert.deepEqual(configuredBackendResources, expectedResources);

  const prepName = "Prepare placeholder Tauri resources for Rust validation";
  const validations = [
    [ciWorkflow, ciJob("rust"), "Run Rust tests"],
    [workflow, job("validate-release"), "Validate Rust desktop graph"],
  ];

  for (const [source, validation, rustStepName] of validations) {
    assert.equal(source.split(prepName).length - 1, 1);
    const prep = step(validation, prepName);
    const preparedResources = [
      ...prep.matchAll(/\.\.\/\.\.\/backend\/[A-Za-z0-9_./-]+/g),
    ]
      .map((match) => match[0])
      .sort();
    assert.deepEqual(preparedResources, expectedResources);
    assert.match(prep, /mkdir -p/);
    assert.doesNotMatch(prep, /touch|printf|frontend\/out/);
    assert.ok(validation.indexOf(prepName) < validation.indexOf(rustStepName));
  }

  assert.match(ciJob("rust"), /working-directory:\s*desktop-tauri\/src-tauri/);
  assert.match(
    step(job("validate-release"), prepName),
    /working-directory:\s*desktop-tauri\/src-tauri/,
  );
  for (const name of ["build-windows", "build-macos", "build-linux", "publish"]) {
    assert.doesNotMatch(job(name), new RegExp(escapeRegExp(prepName)));
  }
});

test("formal platform builds replace validation placeholders with verified real resources", () => {
  const expectations = [
    [
      "build-windows",
      "Build backend with locked PyInstaller",
      "Verify backend bundle",
      "Download verified Node.js runtime",
      "Build Tauri NSIS installer",
    ],
    [
      "build-macos",
      "Build native backend with locked PyInstaller",
      "Verify backend bundle with full smoke",
      "Download verified native Node.js runtime",
      "Build Tauri app for post-copy repair",
    ],
    [
      "build-linux",
      "Build backend with locked PyInstaller",
      "Verify backend bundle",
      "Download verified Node.js runtime",
      "Build Tauri Linux installers",
    ],
  ];

  for (const [jobName, backendName, backendVerifyName, nodeName, tauriName] of
    expectations) {
    const build = job(jobName);
    const orderedSteps = [
      backendName,
      backendVerifyName,
      nodeName,
      "Verify bundled Node.js toolchain",
      tauriName,
    ].map((name) => build.indexOf(`      - name: ${name}`));
    assert.ok(orderedSteps.every((index) => index >= 0), `${jobName} is missing a resource step`);
    assert.deepEqual(orderedSteps, [...orderedSteps].sort((a, b) => a - b));
    assert.match(step(build, backendName), /PyInstaller/);
    assert.match(step(build, backendVerifyName), /verify-bundle\.mjs/);
    assert.match(step(build, nodeName), /download_node\.py/);
    assert.match(
      step(build, "Verify bundled Node.js toolchain"),
      /verify-node-runtime\.mjs backend\/resources\/nodejs/,
    );
    assert.doesNotMatch(build, /placeholder Tauri resources|\.ci-resource-placeholder/);
  }
});

test("v1.1 native builds stage one immutable target before PyInstaller", () => {
  const expectations = [
    [
      "build-windows",
      "Build backend with locked PyInstaller",
      /TARGET="\$OFFICE_RENDERER_TARGET"/,
      /name:\s*suxiaoyou-office-renderer-\$\{\{ matrix\.target \}\}-\$\{\{ github\.sha \}\}/,
      /suxiaoyou-office-renderer-source-\$\{\{ matrix\.target \}\}/,
      /vars\[matrix\.officeLockName\]/,
    ],
    [
      "build-macos",
      "Build native backend with locked PyInstaller",
      /darwin-arm64[^\n]*darwin-x64/,
      /name:[^\n]*darwin-arm64[^\n]*darwin-x64[^\n]*github\.sha/,
      /suxiaoyou-office-renderer-source-[^\n]*darwin-arm64[^\n]*darwin-x64/,
      /SUXIAOYOU_OFFICE_RENDERER_LOCK_SHA256_DARWIN_ARM64[^\n]*SUXIAOYOU_OFFICE_RENDERER_LOCK_SHA256_DARWIN_X64/,
    ],
    [
      "build-linux",
      "Build backend with locked PyInstaller",
      /TARGET="\$\{\{ matrix\.target \}\}"/,
      /name:[^\n]*matrix\.target[^\n]*github\.sha/,
      /suxiaoyou-office-renderer-source-\$\{\{ matrix\.target \}\}/,
      /SUXIAOYOU_OFFICE_RENDERER_LOCK_SHA256_LINUX_ARM64[^\n]*SUXIAOYOU_OFFICE_RENDERER_LOCK_SHA256_LINUX_X64/,
    ],
  ];

  for (const [
    jobName,
    backendStepName,
    targetPattern,
    artifactPattern,
    sourcePathPattern,
    lockPattern,
  ] of
    expectations) {
    const build = job(jobName);
    const materialization = step(
      build,
      "Materialize authenticated Office renderer source for v1.1",
    );
    const staging = step(build, "Stage immutable Office renderer for v1.1");
    const backendBuild = step(build, backendStepName);
    assert.ok(
      build.indexOf("Materialize authenticated Office renderer source for v1.1") <
        build.indexOf("Stage immutable Office renderer for v1.1") &&
        build.indexOf("Stage immutable Office renderer for v1.1") <
        build.indexOf(backendStepName),
      `${jobName} must materialize and stage the renderer before PyInstaller`,
    );
    assert.match(materialization, /if:\s*startsWith\([^\n]*app_version, '1\.1\.'\)/);
    assert.match(
      materialization,
      /uses:\s*actions\/download-artifact@3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c/,
    );
    assert.match(materialization, artifactPattern);
    assert.match(materialization, sourcePathPattern);
    assert.match(
      materialization,
      /repository:\s*\$\{\{ vars\.SUXIAOYOU_OFFICE_RENDERER_ARTIFACT_REPOSITORY \}\}/,
    );
    assert.match(
      materialization,
      /run-id:\s*\$\{\{ vars\.SUXIAOYOU_OFFICE_RENDERER_ARTIFACT_RUN_ID \}\}/,
    );
    assert.match(
      materialization,
      /github-token:\s*\$\{\{ secrets\.SUXIAOYOU_OFFICE_RENDERER_ARTIFACT_TOKEN \}\}/,
    );
    assert.match(staging, /if:\s*startsWith\([^\n]*app_version, '1\.1\.'\)/);
    assert.match(staging, /stage-office-renderer\.mjs stage/);
    assert.match(staging, /authenticated per-commit/);
    assert.match(staging, /reviewed lock SHA-256/);
    assert.match(staging, targetPattern);
    assert.match(staging, sourcePathPattern);
    assert.match(staging, lockPattern);
    assert.doesNotMatch(staging, /curl|wget|base64\s+--decode|openssl|chmod/);
    assert.match(
      backendBuild,
      /SUXIAOYOU_V11_OFFICE_RENDERER_REQUIRED:[^\n]*'1'[^\n]*'0'/,
    );
    assert.match(
      backendBuild,
      /SUXIAOYOU_OFFICE_RENDERER_STAGE:\s*\$\{\{ steps\.office-renderer\.outputs\.stage \}\}/,
    );
    assert.match(
      backendBuild,
      /SUXIAOYOU_OFFICE_RENDERER_TARGET:\s*\$\{\{ steps\.office-renderer\.outputs\.target \}\}/,
    );
    assert.match(
      backendBuild,
      /SUXIAOYOU_OFFICE_RENDERER_LOCK_SHA256:\s*\$\{\{ steps\.office-renderer\.outputs\.lock_sha256 \}\}/,
    );
  }

  assert.match(
    step(job("build-macos"), "Stage immutable Office renderer for v1.1"),
    /nested-code-signed[^\n]*attestation must be generated after signing/,
  );
  assert.doesNotMatch(workflow, /SUXIAOYOU_OFFICE_RENDERER_SOURCE_/);
});

test("native PyInstaller builds pin checkout and staged renderer inputs before and after packaging", () => {
  const builds = [
    ["build-windows", "Build backend with locked PyInstaller"],
    ["build-macos", "Build native backend with locked PyInstaller"],
    ["build-linux", "Build backend with locked PyInstaller"],
  ];

  for (const [jobName, stepName] of builds) {
    const build = step(job(jobName), stepName);
    assert.match(build, /shell:\s*bash/);
    assert.match(build, /set -euo pipefail/);
    assert.match(
      build,
      /expected_commit="\$\(git rev-parse --verify "\$\{GITHUB_SHA\}\^\{commit\}"\)"/,
    );
    assert.match(
      build,
      /actual_commit="\$\(git rev-parse --verify "HEAD\^\{commit\}"\)"/,
    );
    assert.match(build, /"\$actual_commit" != "\$expected_commit"/);
    assert.match(build, /git diff --quiet --ignore-submodules=none --/);
    assert.match(build, /git diff --cached --quiet --ignore-submodules=none --/);
    assert.match(
      build,
      /git status --porcelain=v1 --untracked-files=no --ignore-submodules=none/,
    );
    assert.match(
      build,
      /git ls-files --others --exclude-standard -- backend/,
    );
    assert.match(build, /untracked backend source could shadow frozen imports/);

    assert.match(build, /renderer_stage_fingerprint\(\)/);
    assert.match(build, /root\.rglob\("\*"\)/);
    assert.match(build, /stat\.S_IMODE\(before\.st_mode\)/);
    assert.match(build, /file_digest\.update\(chunk\)/);
    assert.match(build, /Office renderer stage contains a symbolic link/);
    assert.match(
      build,
      /hashlib\.sha256\(lock_bytes\)\.hexdigest\(\) != expected_lock/,
    );
    assert.match(
      build,
      /SUXIAOYOU_OFFICE_RENDERER_LOCK_SHA256/,
    );

    const beforeCheckout = build.indexOf("\n          verify_release_checkout\n");
    const beforeFingerprint = build.indexOf(
      'RENDERER_STAGE_BEFORE="$(renderer_stage_fingerprint)"',
    );
    const pyInstaller = build.indexOf("(cd backend && python -m PyInstaller");
    const afterCheckout = build.indexOf(
      "\n          verify_release_checkout\n",
      beforeCheckout + 1,
    );
    const afterFingerprint = build.indexOf(
      'RENDERER_STAGE_AFTER="$(renderer_stage_fingerprint)"',
    );
    const fingerprintComparison = build.indexOf(
      '"$RENDERER_STAGE_AFTER" != "$RENDERER_STAGE_BEFORE"',
    );
    assert.ok(
      beforeCheckout >= 0 &&
        beforeCheckout < beforeFingerprint &&
        beforeFingerprint < pyInstaller &&
        pyInstaller < afterCheckout &&
        afterCheckout < afterFingerprint &&
        afterFingerprint < fingerprintComparison,
      `${jobName} must verify checkout and renderer inputs immediately around PyInstaller`,
    );
    assert.equal(
      [...build.matchAll(/^\s+verify_release_checkout$/gm)].length,
      2,
      `${jobName} must check the checkout exactly before and after PyInstaller`,
    );
  }
});

test("gates native CI and every release package on durable workspace identity v2", () => {
  assert.match(
    backendProject,
    /workspace_identity_v2: durable workspace identity, migration, and recovery contracts/,
  );

  for (const jobName of ["backend-windows-audit", "backend-macos-execution"]) {
    const nativeJob = ciJob(jobName);
    const gate = step(nativeJob, "Verify durable workspace identity v2");
    assert.match(gate, /python -m pytest -q -m workspace_identity_v2/);
    assert.ok(
      nativeJob.indexOf("Install backend") <
        nativeJob.indexOf("Verify durable workspace identity v2"),
      `${jobName} must install the backend test graph before the identity gate`,
    );
  }

  const releaseBuilds = [
    ["build-windows", "Build backend with locked PyInstaller"],
    ["build-macos", "Build native backend with locked PyInstaller"],
    ["build-linux", "Build backend with locked PyInstaller"],
  ];
  for (const [jobName, stepName] of releaseBuilds) {
    const build = step(job(jobName), stepName);
    const toolInstall = build.indexOf(
      'python -m pip install --require-hashes --only-binary=:all: -r "$RELEASE_TOOL_LOCK"',
    );
    const identityGate = build.indexOf(
      "python -m pytest -q -m workspace_identity_v2 backend/tests",
    );
    const pyInstaller = build.indexOf("(cd backend && python -m PyInstaller");
    assert.ok(
      toolInstall >= 0 &&
        toolInstall < identityGate &&
        identityGate < pyInstaller,
      `${jobName} must pass the identity v2 gate after locked tools are installed and before PyInstaller`,
    );
    assert.equal(
      (build.match(/pytest -q -m workspace_identity_v2 backend\/tests/g) ?? [])
        .length,
      1,
      `${jobName} must run exactly one release identity gate`,
    );
  }
});

test("PyInstaller admits only the lock-bound renderer target and has no ambient fallback", () => {
  assert.match(backendSpec, /office_renderer_datas/);
  assert.match(backendSpec, /\*_required_office_renderer_assets/);
  assert.match(
    officeRendererPackaging,
    /os\.path\.join\("app", "data", "office-renderer", target\)/,
  );
  assert.match(
    officeRendererPackaging,
    /backend\/app\/data\/office-renderer is forbidden/,
  );
  assert.match(officeRendererPackaging, /version_parts >= \(1, 1, 0\)/);
  assert.match(backendSpec, /prepare_frozen_release_identity/);
  assert.match(backendSpec, /\*_release_identity_build\.datas/);
  assert.match(
    backendSpec,
    /release_identity=_release_identity_build\.identity/,
  );
  assert.match(
    officeRendererPackaging,
    /attestation\.get\("app_version"\) != release_identity\.app_version/,
  );
  assert.match(
    officeRendererPackaging,
    /attestation\.get\("release_commit"\) != release_identity\.release_commit/,
  );
  assert.match(
    officeRendererPackaging,
    /target not in SUPPORTED_TARGETS or target != _native_target\(\)/,
  );
  assert.match(
    officeRendererPackaging,
    /Office renderer staging must contain exactly one native target/,
  );
  assert.match(
    officeRendererStager,
    /final-native-bytes-attested-after-signing-v1/,
  );
  assert.match(officeRendererStager, /extra platform bundles are forbidden/);
  assert.match(officeRendererStager, /refusing to merge or overwrite/);
  assert.doesNotMatch(
    officeRendererStager,
    /\b(?:curl|wget)\b|fetch\(|https?:\/\//,
  );
});

test("verifies the complete bundled Node toolchain before every Tauri build", () => {
  const expectations = [
    ["build-windows", "Download verified Node.js runtime", "Build Tauri NSIS installer"],
    ["build-macos", "Download verified native Node.js runtime", "Build Tauri app for post-copy repair"],
    ["build-linux", "Download verified Node.js runtime", "Build Tauri Linux installers"],
  ];

  for (const [jobName, downloadName, buildName] of expectations) {
    const build = job(jobName);
    const downloadIndex = build.indexOf(downloadName);
    const verifyIndex = build.indexOf("Verify bundled Node.js toolchain");
    const tauriBuildIndex = build.indexOf(buildName);
    assert.ok(downloadIndex >= 0, `${jobName} does not download Node`);
    assert.ok(verifyIndex > downloadIndex, `${jobName} verifies Node before download`);
    assert.ok(tauriBuildIndex > verifyIndex, `${jobName} builds Tauri before Node verification`);
    assert.match(
      step(build, "Verify bundled Node.js toolchain"),
      /node scripts\/verify-node-runtime\.mjs backend\/resources\/nodejs/,
    );
  }
});

test("re-extracts Linux installers and executes their packaged Node toolchain", () => {
  const linux = job("build-linux");
  const uploadIndex = linux.indexOf("Upload Linux artifacts");
  const verifyIndex = linux.indexOf(
    "Verify Linux installers and packaged Node.js toolchain",
  );
  assert.ok(uploadIndex >= 0, "Linux artifacts are not uploaded");
  assert.ok(
    uploadIndex > verifyIndex,
    "successful artifact upload must follow installed-content verification",
  );
  assert.match(step(linux, "Upload Linux artifacts"), /if:\s*success\(\)/);
  assert.match(linux, /dpkg-deb -x/);
  assert.match(linux, /DEB_PACKAGE=.*dpkg-deb -f .* Package/);
  assert.match(linux, /dpkg-deb -f .* Version/);
  assert.match(linux, /dpkg-deb -f .* Architecture/);
  assert.match(linux, /DEB_DEPENDS=.*dpkg-deb -f .* Depends/);
  assert.match(linux, /libxdo3\(\[, \(\]\|\$\)/);
  assert.match(linux, /bubblewrap\(\[, \(\]\|\$\)/);
  assert.match(linux, /RPM_PACKAGE=.*rpm -qp --queryformat '%\{NAME\}'/);
  assert.match(linux, /RPM_VERSION=.*rpm -qp --queryformat '%\{VERSION\}'/);
  assert.match(linux, /RPM_ARCH=.*rpm -qp --queryformat '%\{ARCH\}'/);
  assert.match(linux, /RPM_REQUIRES=.*rpm -qp --requires/);
  assert.match(linux, /xdotool/);
  assert.match(linux, /grep -qx 'bubblewrap'/);
  assert.match(linux, /EXPECTED_PACKAGE="suxiaoyou"/);
  assert.match(linux, /DEB package is \$DEB_PACKAGE, expected \$EXPECTED_PACKAGE/);
  assert.match(linux, /RPM package is \$RPM_PACKAGE, expected \$EXPECTED_PACKAGE/);
  assert.match(linux, /EXPECTED_DEB_ARCH:[^\n]*arm64[^\n]*amd64/);
  assert.match(linux, /EXPECTED_RPM_ARCH:[^\n]*aarch64[^\n]*x86_64/);
  assert.match(linux, /expected \$EXPECTED_DEB_ARCH/);
  assert.match(linux, /expected \$EXPECTED_RPM_ARCH/);
  assert.match(linux, /rpm -K/);
  assert.match(linux, /rpm2cpio/);
  assert.match(linux, /rpm2cpio .* > "\$RPM_PAYLOAD"/);
  assert.match(linux, /rpm2cpio_status/);
  assert.match(linux, /cpio -it --quiet/);
  assert.match(linux, /RPM\/CPIO entry-count mismatch/);
  assert.match(linux, /cpio -idm --quiet < "\$RPM_PAYLOAD"/);
  assert.doesNotMatch(linux, /rpm2cpio[^\n]*\|/);
  assert.match(linux, /nodejs\/bin\/node/);
  assert.match(linux, /Expected exactly one packaged Node binary/);
  assert.match(
    linux,
    /node scripts\/verify-node-runtime\.mjs "\$runtime"/,
  );
  assert.match(linux, /backend\/suxiaoyou-backend/);
  assert.match(linux, /Expected exactly one packaged backend/);
  assert.match(linux, /node scripts\/verify-bundle\.mjs/);
});

test("silently installs Windows NSIS and executes its packaged Node toolchain", () => {
  const windows = job("build-windows");
  const dependencies = step(windows, "Install locked JavaScript dependencies");
  assert.match(dependencies, /shell:\s*bash/);
  assert.match(dependencies, /set -euo pipefail/);
  const install = step(
    windows,
    "Install NSIS package and verify packaged Node.js toolchain",
  );
  assert.match(install, /Start-Process/);
  assert.match(install, /-ArgumentList @\("\/S", "\/L=1033", "\/D=\$installDirectory"\)/);
  assert.ok(
    install.indexOf("# Seed the legacy Chinese preference")
      < install.indexOf("$process = Start-Process `"),
    "comments must stay outside the PowerShell line-continuation block",
  );
  assert.match(nsisTemplate, /\$\{GetParameters\} \$R1/);
  assert.match(nsisTemplate, /\$\{GetOptions\} \$R1 "\/L=" \$R0/);
  assert.match(nsisTemplate, /\$R0 == "\$\{LANG_ENGLISH\}"/);
  assert.match(nsisTemplate, /\$R0 == "\$\{LANG_SIMPCHINESE\}"/);
  const explicitLanguageWrite =
    'WriteRegStr HKCU "${MANUPRODUCTKEY}" "Installer Language" $LANGUAGE';
  const muiLanguageInitialization = "!insertmacro MUI_LANGDLL_DISPLAY";
  const initStart = nsisTemplate.indexOf("Function .onInit");
  const installStart = nsisTemplate.indexOf("Section Install");
  const uninstallStart = nsisTemplate.indexOf("Section Uninstall");
  const ordering = [
    nsisTemplate.indexOf(explicitLanguageWrite, initStart),
    nsisTemplate.indexOf(muiLanguageInitialization, initStart),
    nsisTemplate.indexOf('StrCpy $SuyoLocalizedName "suyo"', initStart),
    nsisTemplate.indexOf("!insertmacro NSIS_HOOK_PREINSTALL", installStart),
    nsisTemplate.indexOf(
      'WriteRegStr SHCTX "${UNINSTKEY}" "DisplayName" "$SuyoLocalizedName"',
      installStart,
    ),
    nsisTemplate.indexOf("!insertmacro NSIS_HOOK_POSTINSTALL", installStart),
  ];
  assert.ok(
    ordering.every((position) => position >= 0)
      && ordering.every((position, index) => index === 0 || position > ordering[index - 1]),
    "language seed, MUI initialization, cached name, and installer hooks must stay ordered",
  );
  assert.doesNotMatch(
    nsisTemplate.slice(installStart, uninstallStart),
    /StrCpy \$LANGUAGE/,
    "$LANGUAGE can only select the active NSIS language table during .onInit",
  );
  assert.match(
    nsisTemplate,
    /WriteRegStr HKCU "\$\{MANUPRODUCTKEY\}" "Installer Language" \$LANGUAGE/,
  );
  assert.match(nsisTemplate, /StrCpy \$SuyoLocalizedName "suyo"/);
  assert.match(nsisTemplate, /StrCpy \$SuyoLocalizedName "苏小有"/);
  assert.match(nsisTemplate, /StrCpy \$SuyoLocalizedShortcutName "suyo\.lnk"/);
  assert.match(nsisTemplate, /StrCpy \$SuyoLocalizedShortcutName "苏小有\.lnk"/);
  assert.match(install, /-Value "2052"/);
  assert.match(install, /Installed language is \$installedLanguage, expected 1033/);
  assert.match(install, /require\('\.\/package\.json'\)\.version/);
  assert.match(install, /installer\[0\]\.VersionInfo\.ProductName/);
  assert.match(install, /installer ProductName is \$installerProductName, expected suyo/);
  assert.match(
    install,
    /HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\苏小有/,
  );
  assert.match(install, /uninstallMetadata\.DisplayName -ne "suyo"/);
  assert.match(install, /uninstallMetadata\.InstallLocation/);
  assert.match(install, /GetFullPath\(\$recordedInstallLocation\)/);
  assert.match(install, /GetFullPath\(\$installDirectory\)/);
  assert.match(install, /legacyShortcuts/);
  assert.match(install, /localizedShortcuts/);
  assert.match(install, /CreateShortcut\(\$shortcutSeed\)/);
  assert.match(install, /Move-Item -Force -LiteralPath \$shortcutSeed -Destination \$legacyShortcut/);
  assert.match(install, /Localized shortcut is missing/);
  assert.match(install, /Legacy Chinese shortcut was not migrated/);
  assert.match(
    install,
    /-ArgumentList @\("\/S", "\/UPDATE", "\/L=2052", "\/D=\$installDirectory"\)/,
  );
  assert.match(install, /Chinese DisplayName is \$\(\$uninstallMetadata\.DisplayName\), expected 苏小有/);
  assert.match(install, /Chinese shortcut is missing after language update/);
  assert.match(install, /Copy-Item -Force -LiteralPath \$chineseShortcut -Destination \$shortcutProbe/);
  assert.match(install, /CreateShortcut\(\$shortcutProbe\)\.TargetPath/);
  assert.match(install, /English shortcut remained after Chinese language update/);
  assert.match(
    install,
    /-ArgumentList @\("\/S", "\/UPDATE", "\/L=1033", "\/D=\$installDirectory"\)/,
  );
  assert.match(install, /English DisplayName is \$\(\$uninstallMetadata\.DisplayName\), expected suyo/);
  assert.match(install, /English shortcut is missing after language update/);
  assert.match(install, /Chinese shortcut remained after English language update/);
  assert.match(desktopCargo, /^\[package\]\nname = "suxiaoyou-desktop"$/m);
  assert.equal(tauriConfig.mainBinaryName, undefined);
  assert.match(install, /expectedAppExecutable = "suxiaoyou-desktop\.exe"/);
  assert.match(install, /expectedAppPath = Join-Path \$installDirectory \$expectedAppExecutable/);
  assert.match(install, /appBinary = \$expectedAppPath/);
  assert.match(install, /Test-Path -LiteralPath \$appBinary -PathType Leaf/);
  assert.match(install, /VersionInfo\.ProductVersion/);
  assert.match(install, /Filter node\.exe/);
  assert.match(install, /node scripts\/verify-node-runtime\.mjs/);
  assert.match(install, /Packaged Node\.js runtime verification failed with code \$LASTEXITCODE/);
  assert.match(install, /npm\.cmd/);
  assert.match(install, /npx\.cmd/);
  assert.match(install, /suxiaoyou-backend\.exe/);
  assert.match(install, /node scripts\/verify-bundle\.mjs/);
  assert.match(install, /Installed backend bundle verification failed with code \$LASTEXITCODE/);
  assert.match(install, /preinstallArtifactSizeBefore = \$installer\[0\]\.Length/);
  assert.match(install, /preinstallArtifactSha256 = \(Get-FileHash/);
  assert.match(install, /preinstallCommit = \$env:GITHUB_SHA\.ToLowerInvariant\(\)/);
  assert.ok(
    install.indexOf("$preinstallArtifactSha256 = (Get-FileHash") <
      install.indexOf("$process = Start-Process `"),
  );
  assert.match(install, /verify-desktop-lifecycle\.mjs/);
  assert.match(install, /--executable \$appBinary/);
  assert.match(install, /--artifact \$installer\[0\]\.FullName/);
  assert.match(install, /--artifact-size \$preinstallArtifactSize/);
  assert.match(install, /--artifact-sha256 \$preinstallArtifactSha256/);
  assert.match(install, /--release-commit \$preinstallCommit/);
  assert.doesNotMatch(install, /GITHUB_OUTPUT|steps\.install-windows\.outputs/);
  assert.doesNotMatch(install, /苏小有\.exe/);
});

test("launches every installed desktop, waits for backend ready, and proves clean exit", () => {
  const windowsJob = job("build-windows");
  const windows = step(
    windowsJob,
    "Install NSIS package and verify packaged Node.js toolchain",
  );
  assert.ok(
    windows.indexOf("$preinstallArtifactSha256 = (Get-FileHash") <
      windows.indexOf("$process = Start-Process `"),
  );
  assert.ok(
    windows.indexOf("$process = Start-Process `") <
      windows.indexOf("verify-desktop-lifecycle.mjs"),
  );
  assert.match(windows, /--executable \$appBinary/);
  assert.match(windows, /--artifact \$installer\[0\]\.FullName/);
  assert.match(windows, /--artifact-size \$preinstallArtifactSize/);
  assert.match(windows, /--artifact-sha256 \$preinstallArtifactSha256/);
  assert.match(windows, /--release-commit \$preinstallCommit/);
  assert.match(windows, /verify-desktop-lifecycle\.mjs/);
  assert.match(windows, /suxiaoyou-desktop-lifecycle-\$env:WINDOWS_RELEASE_TARGET/);

  const mac = step(job("build-macos"), "Verify final DMG contents");
  const copiedApp = mac.indexOf("ditto \"$MOUNT_DIRECTORY/苏小有.app\"");
  const lifecycle = mac.indexOf("verify-desktop-lifecycle.mjs");
  const macSeal = mac.indexOf('DMG_PREINSTALL_SHA256="$(shasum -a 256');
  const macMount = mac.indexOf("hdiutil attach");
  assert.ok(macSeal >= 0 && macMount > macSeal);
  assert.ok(copiedApp >= 0 && lifecycle > copiedApp);
  assert.match(mac, /CFBundleExecutable/);
  assert.match(mac, /suxiaoyou-desktop-lifecycle-macos-\$DMG_ARCH/);
  assert.match(mac, /--artifact "\$DMG_PATH"/);
  assert.match(mac, /--artifact-size "\$DMG_PREINSTALL_SIZE"/);
  assert.match(mac, /--artifact-sha256 "\$DMG_PREINSTALL_SHA256"/);
  assert.match(mac, /--release-commit "\$DMG_PREINSTALL_COMMIT"/);

  const linux = step(job("build-linux"), "Install Linux packages and verify desktop lifecycle");
  assert.match(linux, /sudo dpkg -i/);
  assert.match(linux, /sudo rpm -i --nodeps/);
  assert.ok(
    linux.indexOf('DEB_PREINSTALL_SHA256="$(sha256sum') <
      linux.indexOf("sudo dpkg -i"),
  );
  assert.ok(
    linux.indexOf('RPM_PREINSTALL_SHA256="$(sha256sum') <
      linux.indexOf("sudo rpm -i --nodeps"),
  );
  assert.match(linux, /xvfb-run -a dbus-run-session/);
  assert.equal((linux.match(/verify-desktop-lifecycle\.mjs/g) ?? []).length, 2);
  assert.equal((linux.match(/--bundle-type deb/g) ?? []).length, 1);
  assert.equal((linux.match(/--bundle-type rpm/g) ?? []).length, 1);
  assert.equal((linux.match(/--artifact\s/g) ?? []).length, 2);
  assert.equal((linux.match(/--artifact-size/g) ?? []).length, 2);
  assert.equal((linux.match(/--artifact-sha256/g) ?? []).length, 2);
  assert.equal((linux.match(/--release-commit "\$(?:DEB|RPM)_PREINSTALL_COMMIT"/g) ?? []).length, 2);
  assert.match(linux, /sudo dpkg --purge/);
  assert.match(linux, /sudo rpm -e/);

  for (const name of ["build-windows", "build-macos", "build-linux"]) {
    assert.match(job(name), /lifecycle-diagnostics/);
    assert.match(job(name), /if-no-files-found:\s*ignore/);
  }
});

test("aggregates checksum-bound lifecycle reports into scorecard package records", () => {
  const publish = job("publish");
  const generate = step(publish, "Generate scorecard-ready native package evidence");
  assert.match(generate, /git rev-parse 'HEAD\^\{commit\}'/);
  assert.match(generate, /native-package-evidence\.mjs/);
  assert.match(generate, /release-assets/);
  assert.match(generate, /artifacts/);
  assert.match(generate, /CHECKSUMS\.md/);
  assert.match(generate, /"\$GITHUB_REF_NAME"/);
  assert.match(generate, /"\$RELEASE_CHANNEL"/);
  assert.match(generate, /PACKAGE-LIFECYCLE\.json/);

  const upload = step(publish, "Retain native package evidence for RC scorecard");
  assert.match(upload, /v1-native-package-evidence-\$\{\{ github\.run_attempt \}\}/);
  assert.match(upload, /path:\s*PACKAGE-LIFECYCLE\.json/);
  assert.match(upload, /if-no-files-found:\s*error/);
  assert.match(upload, /retention-days:\s*30/);
});

test("Windows native build validates lifecycle primitives before packaging", () => {
  const windows = job("build-windows");
  const buildBackend = step(windows, "Build backend with locked PyInstaller");
  const auditBackend = step(
    windows,
    "Audit Windows backend production dependency graph",
  );

  assert.match(buildBackend, /requirements-release-tools-windows-x64\.txt/);
  assert.match(
    buildBackend,
    /python -m pip install --require-hashes --only-binary=:all: -r "\$RELEASE_TOOL_LOCK"/,
  );
  assert.match(buildBackend, /python -m pytest -q backend\/tests\/test_run\.py/);
  assert.match(buildBackend, /backend\/tests\/test_scripts\/test_download_node\.py/);
  assert.match(
    buildBackend,
    /backend\/tests\/test_tool\/test_windows_mutation_safety\.py/,
  );
  assert.match(
    auditBackend,
    /python -m pip_audit --strict --require-hashes -r "\$AUDIT_LOCK"/,
  );
});

test("Windows ARM64 wheelhouse is sealed, manual-bootstrap-only, and installed offline", () => {
  const windows = job("build-windows");
  const seals = step(windows, "Verify Windows ARM64 wheelhouse input seals");
  const wheelhouse = step(windows, "Build approved Windows ARM64 wheelhouse");
  const buildBackend = step(windows, "Build backend with locked PyInstaller");
  const auditBackend = step(
    windows,
    "Audit Windows backend production dependency graph",
  );
  const diagnostics = step(windows, "Upload Windows lifecycle diagnostics");

  assert.match(seals, /if:\s*matrix\.target == 'windows-arm64'/);
  const sealedInputs = new Map([
    [
      "backend/requirements-windows-arm64.txt",
      "ba428376d5dec539a510a0253595262b30957b9bffbc0c9906179d5a9ee553c8",
    ],
    [
      "backend/requirements-windows-arm64-build.in",
      "cfe41dac66e12c04dedaeb55e7e66c85e0a2c4ca1300b06382730304e98344a7",
    ],
    [
      "backend/requirements-windows-arm64-build.txt",
      "267cd1b54c8824d809691686baa64746cb3c3a315fc89fd40960b1083d894f36",
    ],
    [
      "backend/requirements-release-tools-windows-arm64.txt",
      "c2d9acd90b6229e99b8527bb262002785397ecbfeec4319babca9fa805a4db7f",
    ],
    [
      "backend/requirements-windows-arm64-overrides.txt",
      "8cf1bc48daa284c96510dfb547a0a04e3d681b7670c412709d3db5e07f1b4bd3",
    ],
    [
      "backend/requirements-windows-arm64-sources.json",
      "7083f14d1a0f0805c1da38cba159d5de8a700d2dbc3e0392dfda3ce6d52e4fef",
    ],
    [
      "backend/requirements-windows-arm64-tiktoken.Cargo.lock",
      "0283ef6771d432d962b0ee9483c4259ac5140b4bee77cee97b700192ab52a9e3",
    ],
    [
      "backend/requirements-windows-arm64-wheelhouse-approval.json",
      "1ed4754dfb88bfb72323e3a93dda15b13ae2eac213376bf9a8991aeec6265896",
    ],
    [
      "backend/scripts/build_windows_arm64_wheelhouse.py",
      "3ba6387400b8d34392355aeee2c7d5b0a7a00a61807c1e110096ee5e23003358",
    ],
  ]);
  for (const [path, digest] of sealedInputs) {
    assert.match(
      seals,
      new RegExp(
        `"${escapeRegExp(path)}"\\s*=\\s*"${escapeRegExp(digest)}"`,
      ),
    );
  }
  assert.match(seals, /Get-FileHash -Algorithm SHA256/);
  assert.match(seals, /if \(\$actual -ne \$entry\.Value\)/);
  assert.ok(
    windows.indexOf("- name: Verify Windows ARM64 wheelhouse input seals") <
      windows.indexOf("- name: Build approved Windows ARM64 wheelhouse"),
  );

  assert.match(wheelhouse, /if:\s*matrix\.target == 'windows-arm64'/);
  assert.match(wheelhouse, /RELEASE_EVENT_NAME:\s*\$\{\{ github\.event_name \}\}/);
  assert.match(
    wheelhouse,
    /\$approval\.status -eq "approved"[\s\S]*?\$approvedDigest -match "\^\[0-9a-f\]\{64\}\$"[\s\S]*?\$approvedDigest -ne \$zeroSha256/,
  );
  const dispatchStart = wheelhouse.indexOf(
    'if ($env:RELEASE_EVENT_NAME -eq "workflow_dispatch")',
  );
  const pushStart = wheelhouse.indexOf(
    '} elseif ($env:RELEASE_EVENT_NAME -eq "push")',
  );
  const unsupportedStart = wheelhouse.indexOf("} else {", pushStart);
  assert.ok(dispatchStart >= 0);
  assert.ok(pushStart > dispatchStart);
  assert.ok(unsupportedStart > pushStart);
  const dispatchBranch = wheelhouse.slice(dispatchStart, pushStart);
  const pushBranch = wheelhouse.slice(pushStart, unsupportedStart);
  assert.match(dispatchBranch, /\$approval\.status -eq "bootstrap-required"/);
  assert.match(dispatchBranch, /\$approvedDigest -eq \$zeroSha256/);
  assert.match(dispatchBranch, /\$arguments \+= "--bootstrap"/);
  assert.equal((wheelhouse.match(/--bootstrap/g) ?? []).length, 1);
  assert.doesNotMatch(pushBranch, /--bootstrap/);
  assert.match(pushBranch, /if \(-not \$validApprovedDigest\)/);
  assert.match(
    pushBranch,
    /tagged Windows ARM64 releases require a nonzero tracked approved content SHA-256/,
  );
  assert.match(
    wheelhouse,
    /backend\/scripts\/build_windows_arm64_wheelhouse\.py[\s\S]*?"build"[\s\S]*?"--output"/,
  );
  assert.match(wheelhouse, /windows-arm64-wheelhouse-manifest\.json/);
  assert.match(wheelhouse, /manifest_digest=\$manifestDigest/);

  const armBranchStart = buildBackend.indexOf(
    'if [[ "${{ matrix.target }}" == "windows-arm64" ]]; then',
  );
  const x64BranchStart = buildBackend.indexOf("\n          else", armBranchStart);
  assert.ok(armBranchStart >= 0);
  assert.ok(x64BranchStart > armBranchStart);
  const armBranch = buildBackend.slice(armBranchStart, x64BranchStart);
  for (const flag of [
    "--no-index",
    "--no-deps",
    "--require-hashes",
    "--only-binary=:all:",
  ]) {
    assert.equal(
      (armBranch.match(new RegExp(escapeRegExp(flag), "g")) ?? []).length,
      2,
      `${flag} must protect both Windows ARM64 offline installs`,
    );
  }
  assert.match(
    armBranch,
    /--find-links "\$WHEELHOUSE_ROOT\/production-wheelhouse"/,
  );
  assert.match(
    armBranch,
    /install-locks\/requirements-windows-arm64-install\.txt/,
  );
  assert.match(
    armBranch,
    /--find-links "\$WHEELHOUSE_ROOT\/release-tools-wheelhouse"/,
  );
  assert.match(
    armBranch,
    /install-locks\/requirements-release-tools-windows-arm64-install\.txt/,
  );

  assert.match(
    auditBackend,
    /windows-arm64[\s\S]*?AUDIT_LOCK="backend\/requirements-windows-arm64\.txt"/,
  );
  assert.match(
    auditBackend,
    /python -m pip_audit --strict --require-hashes -r "\$AUDIT_LOCK"/,
  );
  assert.match(
    diagnostics,
    /windows-arm64-wheelhouse-manifest\.json(?:\s|$)/,
  );
  assert.match(
    diagnostics,
    /windows-arm64-wheelhouse-manifest\.json\.sha256/,
  );
});

test("uses Python 3.12 and full backend smoke on every build host", () => {
  const expectedPython = new Map([
    ["build-windows", "3.12.10"],
    ["build-linux", "3.12.13"],
  ]);
  for (const [name, version] of expectedPython) {
    const build = job(name);
    assert.match(
      build,
      new RegExp(`python-version:\\s*"${version.replaceAll(".", "\\.")}"`),
    );
    assert.match(build, /node scripts\/verify-bundle\.mjs/);
    assert.doesNotMatch(build, /VERIFY_BUNDLE_SKIP_SMOKE/);
  }

  const mac = job("build-macos");
  assert.match(
    mac,
    /astral-sh\/setup-uv@11f9893b081a58869d3b5fccaea48c9e9e46f990/,
  );
  assert.match(mac, /version:\s*"0\.11\.28"/);
  assert.match(mac, /uv venv --python 3\.12\.13 --managed-python --seed/);
  assert.match(mac, /platform\.machine\(\)/);
  assert.match(mac, /node scripts\/verify-bundle\.mjs/);
  assert.doesNotMatch(mac, /actions\/setup-python/);
  assert.doesNotMatch(mac, /VERIFY_BUNDLE_SKIP_SMOKE/);
});

test("CI uses the portable arm64 Python path for macOS execution safety", () => {
  const mac = ciJob("backend-macos-execution");

  assert.match(mac, /runs-on:\s*macos-14/);
  assert.match(
    mac,
    /astral-sh\/setup-uv@11f9893b081a58869d3b5fccaea48c9e9e46f990/,
  );
  assert.match(mac, /version:\s*"0\.11\.28"/);
  assert.match(mac, /uv venv --python 3\.12\.13 --managed-python --seed/);
  assert.match(mac, /platform\.machine\(\)/);
  assert.doesNotMatch(mac, /actions\/setup-python/);
});

test("runs the frozen Office create-edit-reopen contract on every native target", () => {
  const nativeSteps = [
    ["build-windows", "Verify backend bundle", /VERIFY_BUNDLE_OFFICE_PLATFORM:\s*\$\{\{ matrix\.target \}\}/],
    [
      "build-linux",
      "Verify backend bundle",
      /VERIFY_BUNDLE_OFFICE_PLATFORM:\s*\$\{\{ matrix\.target \}\}/,
    ],
  ];
  for (const [jobName, stepName, platformPattern] of nativeSteps) {
    const verification = step(job(jobName), stepName);
    assert.match(verification, platformPattern);
    assert.match(verification, /node scripts\/verify-bundle\.mjs/);
  }
  const mac = job("build-macos");
  const macVerification = step(mac, "Verify backend bundle with full smoke");
  assert.match(
    mac,
    /VERIFY_BUNDLE_OFFICE_PLATFORM:[^\n]*macos-arm64[^\n]*macos-x64/,
  );
  assert.match(macVerification, /node scripts\/verify-bundle\.mjs/);

  assert.match(bundleVerifier, /--office-self-test/);
  assert.match(bundleVerifier, /resolveCheckoutCommit\(\)/);
  assert.match(bundleVerifier, /SUXIAOYOU_RELEASE_COMMIT:\s*expectedCommit/);
  assert.match(bundleVerifier, /validateOfficeContractReport/);
  assert.match(bundleVerifier, /requireFrozen:\s*true/);

  const publish = job("publish");
  const attemptSelection = step(
    publish,
    "Select latest evidence attempt per native target",
  );
  for (const prefix of [
    "windows-x64-lifecycle-diagnostics",
    "windows-arm64-lifecycle-diagnostics",
    "macos-aarch64-lifecycle-diagnostics",
    "macos-x64-lifecycle-diagnostics",
    "linux-x64-lifecycle-diagnostics",
    "linux-arm64-lifecycle-diagnostics",
    "v1-integration-evidence",
  ]) {
    assert.match(attemptSelection, new RegExp(escapeRegExp(prefix)));
  }
  assert.match(attemptSelection, /sort -V/);
  assert.match(attemptSelection, /rm -rf -- "\$candidate"/);
  const officeEvidence = step(publish, "Verify native Office compatibility evidence");
  assert.match(officeEvidence, /office-contract-evidence\.mjs/);
  assert.match(officeEvidence, /aggregate/);
  assert.match(officeEvidence, /git rev-parse 'HEAD\^\{commit\}'/);
  assert.match(officeEvidence, /"\$GITHUB_REF_NAME"/);
  assert.match(officeEvidence, /"\$OFFICE_COMPATIBILITY_ASSET"/);
  assert.match(
    publish,
    /OFFICE_COMPATIBILITY_ASSET:[^\n]*'OFFICE-RESTRICTED-COMPATIBILITY\.json'[^\n]*'OFFICE-COMPATIBILITY\.json'/,
  );
  const release = step(publish, "Prepare GitHub Release");
  assert.match(release, /files:[\s\S]*\$\{\{ env\.OFFICE_COMPATIBILITY_ASSET \}\}/);

  const packagedSteps = [
    ["build-windows", "Install NSIS package and verify packaged Node.js toolchain", /\$\{\{ matrix\.target \}\}/],
    [
      "build-macos",
      "Verify final DMG contents",
      /macos-arm64[^\n]*macos-x64/,
    ],
    [
      "build-linux",
      "Verify Linux installers and packaged Node.js toolchain",
      /\$\{\{ matrix\.target \}\}/,
    ],
  ];
  for (const [jobName, stepName, platformPattern] of packagedSteps) {
    const verification = step(job(jobName), stepName);
    assert.match(verification, /VERIFY_BUNDLE_OFFICE_PLATFORM:/);
    assert.match(verification, platformPattern);
    assert.match(verification, /VERIFY_BUNDLE_OFFICE_REPORT:/);
    assert.match(verification, /VERIFY_BUNDLE_OFFICE_RENDERER_REPORT:/);
    assert.match(verification, /office-contract\.json/);
    assert.match(verification, /office-renderer\.json/);
    assert.match(verification, /verify-bundle\.mjs|verify-macos-bundle\.mjs/);
  }
});

test("validates release metadata, production audits, and tests before building", () => {
  const validate = job("validate-release");
  const install = step(validate, "Install validation dependencies");
  const backend = step(validate, "Audit and test backend production graph");

  assert.match(
    install,
    /python -m pip install --require-hashes --only-binary=cryptography -r backend\/requirements\.txt/,
  );
  assert.match(install, /python -m pip install -e \.\/backend --no-deps/);
  assert.match(install, /requirements-release-tools-linux-x64\.txt/);
  assert.match(
    install,
    /python -m pip install --require-hashes --only-binary=:all: -r "\$RELEASE_TOOL_LOCK"/,
  );
  assert.match(install, /cargo install cargo-audit --locked --version 0\.22\.2/);
  assert.doesNotMatch(install, /backend\[(?:dev|mcp)/);
  assert.match(validate, /node scripts\/release-metadata\.mjs/);
  assert.match(validate, /node --test scripts\/\*\.test\.mjs/);
  assert.match(validate, /npm audit --omit=dev/);
  assert.match(validate, /npm --prefix frontend audit --omit=dev/);
  assert.match(validate, /npm --prefix desktop-tauri audit --omit=dev/);
  assert.match(validate, /node --test tests\/unit\/\*\.test\.ts/);
  assert.match(validate, /playwright install --with-deps chromium/);
  assert.match(validate, /npm run test:ui:core/);
  assert.match(backend, /python -m pip check/);
  assert.match(backend, /pip-audit -r requirements\.txt/);
  assert.match(backend, /pytest -q/);
  assert.match(validate, /cargo metadata --locked --format-version 1/);
  assert.match(validate, /cargo audit --file Cargo\.lock/);
  assert.match(validate, /cargo test --locked/);
  assert.match(validate, /cargo clippy --locked --all-targets -- -D warnings/);

  const releaseToolLocks = new Map([
    [
      "build-windows",
      [
        "requirements-release-tools-windows-x64.txt",
        "requirements-release-tools-windows-arm64.txt",
      ],
    ],
    [
      "build-macos",
      [
        "requirements-release-tools-macos-arm64.txt",
        "requirements-release-tools-macos-x64.txt",
      ],
    ],
    [
      "build-linux",
      [
        "requirements-release-tools-linux-arm64.txt",
        "requirements-release-tools-linux-x64.txt",
      ],
    ],
  ]);
  for (const [name, lockNames] of releaseToolLocks) {
    const build = job(name);
    assert.match(job(name), /needs:\s*validate-release/);
    assert.match(
      build,
      /python -m pip install --require-hashes --only-binary=cryptography -r backend\/requirements\.txt/,
    );
    assert.match(
      build,
      /python -m pip install --require-hashes --only-binary=:all: -r "\$RELEASE_TOOL_LOCK"/,
    );
    for (const lockName of lockNames) {
      assert.match(build, new RegExp(lockName.replaceAll(".", "\\.")));
    }
    assert.doesNotMatch(
      build,
      /python -m pip install (?:pyinstaller|pytest|pytest-asyncio|pip-audit)==/,
      "release tooling must only be installed from a complete hash lock",
    );
  }
});

test("hash-locks the complete release Python toolchain for every native target", async () => {
  const input = readFileSync(
    join(root, "backend/requirements-release-tools.in"),
    "utf8",
  );
  const directRequirements = new Map([
    ["pip-audit", "2.10.1"],
    ["pyinstaller", "6.21.0"],
    ["pytest", "9.1.1"],
    ["pytest-asyncio", "1.4.0"],
  ]);
  assert.deepEqual(
    input
      .split("\n")
      .filter((line) => line && !line.startsWith("#")),
    [...directRequirements].map(([name, version]) => `${name}==${version}`),
  );
  for (const [name, version] of directRequirements) {
    assert.match(
      input,
      new RegExp(`^${name}==${version.replaceAll(".", "\\.")}$`, "m"),
    );
  }

  const lockTargets = [
    [
      "windows-x64",
      "3.12.10",
      "x86_64-pc-windows-msvc",
      "f2e389ca75cf41fe8a45bc40ab5eaffaa4808c3a572f49bccc419c93450c8f9b",
      "backend/requirements.txt",
    ],
    [
      "windows-arm64",
      "3.12.10",
      "aarch64-pc-windows-msvc",
      "c2d9acd90b6229e99b8527bb262002785397ecbfeec4319babca9fa805a4db7f",
      "backend/requirements-windows-arm64.txt",
    ],
    [
      "macos-arm64",
      "3.12.13",
      "aarch64-apple-darwin",
      "332b2249dfe9403ddc7fa1b23b2646e18777ebfd668d8543b022ff8a69b9a5d6",
      "backend/requirements.txt",
    ],
    [
      "macos-x64",
      "3.12.13",
      "x86_64-apple-darwin",
      "e31a1f9bd91c64ad7805badd3d1a71d1cfc37211b28263d6930b0c3d154af5a7",
      "backend/requirements.txt",
    ],
    [
      "linux-x64",
      "3.12.13",
      "x86_64-unknown-linux-gnu",
      "208305ef117280fed096c83876895872110ac23aa4df34b579f5b73f8236d6f3",
      "backend/requirements.txt",
    ],
    [
      "linux-arm64",
      "3.12.13",
      "aarch64-unknown-linux-gnu",
      "773f5855f7b5d6a4395adc095064e0f7abb16067d7fba0b9d7af3343be290d5b",
      "backend/requirements.txt",
    ],
  ];
  const normalizePackageName = (name) =>
    name.toLowerCase().replaceAll(/[_.]+/g, "-");
  for (const [
    target,
    pythonVersion,
    pythonPlatform,
    expectedDigest,
    constraintPath,
  ] of lockTargets) {
    const lock = readFileSync(
      join(root, `backend/requirements-release-tools-${target}.txt`),
      "utf8",
    );
    const productionLock = readFileSync(join(root, constraintPath), "utf8");
    const productionPins = new Map(
      [
        ...productionLock.matchAll(
          /^([A-Za-z0-9_.-]+)==([^\s;]+).*\\$/gm,
        ),
      ].map(([, name, version]) => [normalizePackageName(name), version]),
    );
    assert.match(lock, /uvx --from uv==0\.11\.28 uv pip compile/);
    assert.match(
      lock,
      new RegExp(`--constraints ${escapeRegExp(constraintPath)}`),
    );
    assert.match(
      lock,
      new RegExp(`--python-version ${pythonVersion.replaceAll(".", "\\.")}`),
    );
    assert.match(lock, new RegExp(`--python-platform ${pythonPlatform}`));
    assert.match(lock, /--generate-hashes --only-binary=:all:/);
    assert.match(lock, /--exclude-newer 2026-07-18T00:00:00Z/);
    for (const [name, version] of directRequirements) {
      assert.match(
        lock,
        new RegExp(`^${name}==${version.replaceAll(".", "\\.")} \\\\$`, "m"),
      );
    }
    const requirementStanzas = lock
      .split(/(?=^[a-z0-9][a-z0-9._-]*==)/m)
      .slice(1);
    assert.ok(requirementStanzas.length >= directRequirements.size);
    for (const stanza of requirementStanzas) {
      assert.match(stanza, /--hash=sha256:[0-9a-f]{64}/);
    }
    for (const [, name, version] of lock.matchAll(
      /^([A-Za-z0-9_.-]+)==([^\s;]+).*\\$/gm,
    )) {
      const productionVersion = productionPins.get(normalizePackageName(name));
      if (productionVersion !== undefined) {
        assert.equal(
          version,
          productionVersion,
          `${target} release tooling changes production dependency ${name}`,
        );
      }
    }

    const digestBytes = await globalThis.crypto.subtle.digest(
      "SHA-256",
      new TextEncoder().encode(lock),
    );
    const actualDigest = Buffer.from(digestBytes).toString("hex");
    assert.equal(actualDigest, expectedDigest);
    assert.match(workflow, new RegExp(expectedDigest));
  }
});

test("publishes a dynamically validated cargo-audit category disclosure", () => {
  const validate = job("validate-release");
  const rust = step(validate, "Validate Rust desktop graph");
  assert.match(rust, /cargo audit --file Cargo\.lock --json/);
  assert.match(
    rust,
    /> \.\.\/\.\.\/release-validation\/cargo-audit-report\.json/,
  );
  assert.match(
    rust,
    /cargo-audit-summary\.mjs[\s\\]*generate[\s\\]*\.\.\/\.\.\/release-validation\/cargo-audit-report\.json[\s\\]*\.\.\/\.\.\/release-validation\/cargo-audit-summary\.json/,
  );
  assert.match(
    rust,
    /cargo-audit-summary\.mjs[\s\\]*verify \.\.\/\.\.\/release-validation\/cargo-audit-summary\.json/,
  );
  assert.match(
    rust,
    /cargo-audit-summary\.mjs[\s\\]*assert-clean \.\.\/\.\.\/release-validation\/cargo-audit-summary\.json/,
  );
  assert.match(rust, /AUDIT_STATUS=0/);
  assert.match(rust, /if \(\( AUDIT_STATUS != 0 \)\)/);

  const upload = step(validate, "Upload Rust audit summary");
  assert.match(upload, /if:\s*always\(\)/);
  assert.match(upload, /uses: actions\/upload-artifact@/);
  assert.match(upload, /name:\s*cargo-audit-summary/);
  assert.match(upload, /path:\s*release-validation\/cargo-audit-summary\.json/);
  assert.match(upload, /overwrite:\s*true/);
  assert.match(upload, /if-no-files-found:\s*error/);
  assert.match(upload, /retention-days:\s*1/);

  const publish = job("publish");
  assert.match(publish, /uses: actions\/download-artifact@/);
  assert.match(publish, /path:\s*artifacts/);
  const completeness = step(publish, "Verify artifact completeness");
  assert.match(
    completeness,
    /require_one "Rust cargo-audit summary" artifacts\/cargo-audit-summary "cargo-audit-summary\.json"/,
  );
  const disclosure = step(publish, "Validate and render Rust audit disclosure");
  assert.match(disclosure, /cargo-audit-summary\.mjs verify "\$SUMMARY"/);
  assert.match(disclosure, /cargo-audit-summary\.mjs assert-clean "\$SUMMARY"/);
  assert.match(
    disclosure,
    /cargo-audit-summary\.mjs markdown "\$SUMMARY" RUST-AUDIT\.md/,
  );
  assert.match(step(publish, "Record installer trust status"), /cat RUST-AUDIT\.md/);
  assert.match(
    step(publish, "Prepare GitHub Release"),
    /draft:\s*\$\{\{ needs\.validate-release\.outputs\.release_profile != 'unsigned-degraded' \}\}/,
  );
});

test("pins the complete packaged backend graph including native providers", () => {
  for (const requirement of [
    "mcp==1.28.1",
    "croniter==6.2.4",
    "wcmatch==11.0",
    "readabilipy==0.3.0",
    "markdownify==1.2.3",
    "beautifulsoup4==4.15.0",
    "soupsieve==2.8.4",
    "six==1.17.0",
    "webencodings==0.5.1",
    "lxml==6.1.1",
    "reportlab==4.4.5",
    "cryptography==48.0.1",
    "anthropic==0.116.0",
    "google-genai==2.11.0",
    "keyring==25.7.0",
    "click==8.4.2",
  ]) {
    assert.match(
      backendRequirements,
      new RegExp(`^${escapeRegExp(requirement)}\\s*\\\\$`, "m"),
    );
  }
  const requirementLines = backendRequirements
    .split("\n")
    .filter((line) => line && !line.startsWith(" ") && !line.startsWith("#"));
  assert.ok(requirementLines.length > 50, "backend lock is unexpectedly incomplete");
  for (const line of requirementLines) {
    assert.match(line, /^[A-Za-z0-9_.-]+==[^\s]+(?:\s+;\s+.+)?\s+\\$/);
  }

  for (const requirement of [
    "mcp==1.28.1",
    "croniter==6.2.4",
    "wcmatch==11.0",
    "readabilipy==0.3.0",
    "markdownify==1.2.3",
    "cryptography==48.0.1",
    "beautifulsoup4>=4.12.0",
    "reportlab>=4.4.0",
    "qrcode[pil]>=8.0",
    "websockets>=14.0",
    "click>=8.3.3",
    "anthropic==0.116.0",
    "google-genai==2.11.0",
    "keyring==25.7.0",
  ]) {
    assert.match(backendProject, new RegExp(`"${escapeRegExp(requirement)}"`));
  }
  assert.doesNotMatch(
    `${backendProject}\n${backendRequirements}`,
    /xhtml2pdf|python-bidi|svglib|pyHanko|pyhanko-certvalidator/i,
  );
  assert.doesNotMatch(backendProject, /yakagent/i);
  assert.doesNotMatch(backendProject, /^mcp\s*=\s*\[/m);
});

test("final bundle proves native provider and sandbox entrypoints", () => {
  for (const packageName of ["anthropic", "google.genai", "keyring"]) {
    assert.match(backendSpec, new RegExp(`collect_all\\('${escapeRegExp(packageName)}'\\)`));
  }
  assert.match(backendSpec, /def production_package_only\(datas, hiddenimports\)/);
  assert.match(backendSpec, /part in \{'tests', 'testing'\}/);
  assert.match(backendSpec, /part\.startswith\('_test_'\)/);
  for (const hiddenImport of [
    "app.provider.anthropic_provider",
    "app.provider.gemini_provider",
    "app.tool.sandbox_self_test",
    "app.tool.sandbox_worker",
  ]) {
    assert.match(backendSpec, new RegExp(`'${escapeRegExp(hiddenImport)}'`));
  }
  assert.match(backendEntrypoint, /--provider-self-test/);
  assert.match(backendEntrypoint, /AsyncAnthropic/);
  assert.match(backendEntrypoint, /genai\.Client/);
  assert.match(bundleVerifier, /\["--provider-self-test"\]/);
  assert.match(bundleVerifier, /\["--sandbox-self-test", workspace\]/);
  assert.match(bundleVerifier, /report\.status !== "ok" \|\| report\.platform !== platform/);
  assert.match(bundleVerifier, /windows-job-object/);
  assert.match(bundleVerifier, /direct-approved/);
  assert.match(bundleVerifier, /filesystem_isolated/);
  assert.match(bundleVerifier, /network_isolated/);
  assert.match(bundleVerifier, /environment_sanitized/);
  assert.match(bundleVerifier, /descendant_terminated/);
  assert.match(bundleVerifier, /process_tree_reaped/);
});

test("native bundle verification rejects half-open v1.1 gate graphs", () => {
  for (const jobName of ["build-windows", "build-macos", "build-linux"]) {
    const context = job(jobName);
    assert.match(context, /VERIFY_BUNDLE_V11_GATE_MODE:/);
    assert.match(
      context,
      /startsWith\(needs\.validate-release\.outputs\.app_version, '1\.1\.'\)/,
    );
    assert.match(context, /&& 'released' \|\| 'closed'/);
  }
  assert.match(bundleVerifier, /report\.gate_mode !== expectedGateMode/);
  assert.match(bundleVerifier, /report\.gates_released !== expectedReleased/);
  assert.match(bundleVerifier, /status\.missing_dependencies\.length === 0/);
});

test("publishes reviewed v1.1 patch lines and RC iterations as synchronized unsigned-degraded public prereleases", () => {
  const validate = job("validate-release");
  const context = step(validate, "Resolve and validate release context");
  assert.match(
    validate,
    /release_profile:\s*\$\{\{ steps\.release-context\.outputs\.release_profile \}\}/,
  );
  assert.match(context, /1\.1\.\*\)[\s\S]*?RELEASE_PROFILE="unsigned-degraded"/);
  assert.match(
    context,
    /RELEASE_PROFILE" == "unsigned-degraded"[\s\S]*?RELEASE_CHANNEL="stable"[\s\S]*?IS_STABLE="false"[\s\S]*?MACOS_ARTIFACT_PROFILE="UNSIGNED-DEGRADED"/,
  );
  assert.match(context, /echo "release_profile=\$RELEASE_PROFILE"/);
  assert.doesNotMatch(validate, /Block v1\.1 tags/);

  const runMarker = "        run: |\n";
  const contextScript = context
    .slice(context.indexOf(runMarker) + runMarker.length)
    .split("\n")
    .map((line) => line.replace(/^ {10}/u, ""))
    .join("\n");
  const outputDirectory = mkdtempSync(
    join(tmpdir(), "suxiaoyou-release-workflow-"),
  );
  let resolvedOutput;
  try {
    const githubOutput = join(outputDirectory, "github-output");
    const resolved = spawnSync("bash", ["-c", contextScript], {
      cwd: root,
      encoding: "utf8",
      env: {
        ...process.env,
        GITHUB_EVENT_NAME: "push",
        GITHUB_REF: "refs/tags/v1.1.1",
        GITHUB_REF_NAME: "v1.1.1",
        GITHUB_OUTPUT: githubOutput,
      },
    });
    assert.equal(resolved.status, 0, resolved.stderr);
    resolvedOutput = readFileSync(githubOutput, "utf8");
  } finally {
    rmSync(outputDirectory, { recursive: true, force: true });
  }
  for (const output of [
    "app_version=1.1.1",
    "release_version=1.1.1",
    "release_channel=stable",
    "release_profile=unsigned-degraded",
    "evidence_contract=v1.1",
    "is_stable=false",
    "macos_artifact_profile=UNSIGNED-DEGRADED",
  ]) {
    assert.match(resolvedOutput, new RegExp(`^${output}$`, "mu"));
  }

  const rcOutputDirectory = mkdtempSync(
    join(tmpdir(), "suxiaoyou-release-workflow-rc-"),
  );
  try {
    const githubOutput = join(rcOutputDirectory, "github-output");
    const resolved = spawnSync("bash", ["-c", contextScript], {
      cwd: root,
      encoding: "utf8",
      env: {
        ...process.env,
        GITHUB_EVENT_NAME: "push",
        GITHUB_REF: "refs/tags/v1.1.1-rc.1",
        GITHUB_REF_NAME: "v1.1.1-rc.1",
        GITHUB_OUTPUT: githubOutput,
      },
    });
    assert.equal(resolved.status, 0, resolved.stderr);
    const output = readFileSync(githubOutput, "utf8");
    for (const expected of [
      "app_version=1.1.1",
      "release_version=1.1.1-rc.1",
      "release_channel=prerelease",
      "release_profile=unsigned-degraded",
      "evidence_contract=v1.1",
      "is_stable=false",
      "macos_artifact_profile=UNSIGNED-DEGRADED",
    ]) {
      assert.match(output, new RegExp(`^${expected}$`, "mu"));
    }
  } finally {
    rmSync(rcOutputDirectory, { recursive: true, force: true });
  }

  const preflight = step(
    validate,
    "Preflight v1.1 Office renderer CI configuration",
  );
  assert.match(preflight, /release_profile == 'authoritative'/);
  const appleCredentials = step(
    validate,
    "Require Apple credentials for a stable tag",
  );
  assert.match(appleCredentials, /release_profile == 'authoritative'/);

  const backendSteps = new Map([
    ["build-windows", "Build backend with locked PyInstaller"],
    ["build-macos", "Build native backend with locked PyInstaller"],
    ["build-linux", "Build backend with locked PyInstaller"],
  ]);
  for (const [jobName, backendStepName] of backendSteps) {
    const build = job(jobName);
    for (const rendererStepName of [
      "Materialize authenticated Office renderer source for v1.1",
      "Stage immutable Office renderer for v1.1",
    ]) {
      assert.match(
        step(build, rendererStepName),
        /if:[^\n]*app_version[^\n]*release_profile == 'authoritative'/,
      );
    }
    assert.match(
      build,
      /VERIFY_BUNDLE_V11_GATE_MODE:[^\n]*&& 'released' \|\| 'closed'/,
    );
    assert.match(
      build,
      /VERIFY_BUNDLE_OFFICE_RENDERER_PROFILE:[^\n]*'unsigned-degraded'[^\n]*'signed-authoritative'/,
    );
    const backend = step(build, backendStepName);
    assert.match(
      backend,
      /SUXIAOYOU_V11_OFFICE_RENDERER_REQUIRED:[^\n]*app_version, '1\.1\.'\)[^\n]*'1' \|\| '0'/,
    );
    assert.match(
      backend,
      /SUXIAOYOU_OFFICE_RENDERER_PROFILE:[^\n]*'unsigned-degraded'[^\n]*'signed-authoritative'/,
    );
    assert.match(
      backend,
      /if \[\[ -z "\$\{SUXIAOYOU_OFFICE_RENDERER_PROFILE:-\}" \]\]; then[\s\S]*?unset SUXIAOYOU_OFFICE_RENDERER_PROFILE/,
    );
    assert.match(
      backend,
      /SUXIAOYOU_OFFICE_RENDERER_PROFILE:-\}" == "unsigned-degraded"[\s\S]*?unexpected Office renderer input[\s\S]*?unsigned-degraded-no-renderer/,
    );
    assert.ok(
      backend.indexOf('== "unsigned-degraded"') <
        backend.indexOf('SUXIAOYOU_V11_OFFICE_RENDERER_REQUIRED:-}" != "1"'),
      `${jobName} must short-circuit the absent degraded renderer before authoritative fingerprinting`,
    );
  }
  assert.doesNotMatch(workflow, /SUXIAOYOU_V11_DEGRADED_RELEASE/);
  assert.doesNotMatch(workflow, /released-degraded/);

  const mac = job("build-macos");
  assert.match(mac, /MACOS_ARTIFACT_PROFILE:\s*\$\{\{ needs\.validate-release\.outputs\.macos_artifact_profile \}\}/);
  assert.match(
    step(mac, "Verify RC ad-hoc trust boundary"),
    /is_stable == 'false'/,
  );
  for (const appleStepName of [
    "Import Apple certificate and discover signing identity",
    "Sign final DMG",
    "Notarize and staple final DMG",
    "Verify official signatures and Gatekeeper",
  ]) {
    assert.match(
      step(mac, appleStepName),
      /release_profile == 'authoritative'/,
    );
  }

  const publish = job("publish");
  assert.match(publish, /RELEASE_PROFILE:\s*\$\{\{ needs\.validate-release\.outputs\.release_profile \}\}/);
  assert.match(publish, /OFFICE-RESTRICTED-COMPATIBILITY\.json/);
  assert.match(publish, /DEGRADED-CAPABILITIES\.json/);
  assert.match(
    step(publish, "Generate scorecard-ready native package evidence"),
    /PROFILE_ARGUMENTS=\("\$RELEASE_PROFILE"\)/,
  );
  const manifest = step(
    publish,
    "Generate and verify manual-download release manifest",
  );
  assert.equal((manifest.match(/"\$\{PROFILE_ARGUMENTS\[@\]\}"/g) ?? []).length, 2);

  const disclosure = step(
    publish,
    "Generate unsigned-degraded capability disclosure",
  );
  assert.match(disclosure, /if:\s*env\.RELEASE_PROFILE == 'unsigned-degraded'/);
  assert.match(disclosure, /kind:\s*"suxiaoyou-degraded-capabilities"/);
  assert.match(disclosure, /publicationChannel:\s*"prerelease"/);
  assert.match(disclosure, /publicPrerelease:\s*true/);
  assert.match(disclosure, /draft:\s*false/);
  assert.match(disclosure, /installerCount:\s*8/);
  assert.match(disclosure, /metadataCount:\s*4/);
  assert.match(disclosure, /workspaceIdentityScheme:\s*"stat-v1"/);
  assert.match(disclosure, /deletedDirectoryIdentifierReuse:\s*"not-detectable"/);
  assert.match(disclosure, /authoritativeRenderer:\s*"absent"/);
  assert.match(disclosure, /highFidelityPreview:\s*"unavailable"/);
  assert.match(disclosure, /macosAppSignature:\s*"adhoc"/);
  assert.match(disclosure, /windowsAuthenticode:\s*false/);

  const trust = step(publish, "Record installer trust status");
  assert.match(trust, /UNSIGNED-DEGRADED/);
  assert.match(trust, /不含权威 Office renderer/);
  assert.match(trust, /高保真预览、高保真编辑与视觉提交/);
  assert.match(trust, /Windows NSIS 未配置 Authenticode/);
  assert.match(trust, /macOS 应用仅使用 ad-hoc 签名/);
  assert.match(trust, /Release 公开为 prerelease/);
  assert.match(trust, /stat-v1.*不能识别文件系统在删除目录后立即复用/u);

  const release = step(publish, "Prepare GitHub Release");
  assert.match(release, /name:[^\n]*UNSIGNED-DEGRADED/);
  assert.match(release, /draft:[^\n]*release_profile != 'unsigned-degraded'/);
  assert.match(release, /prerelease:[^\n]*release_profile == 'unsigned-degraded'/);
  assert.match(release, /make_latest:\s*false/);
});

test("verifies eight installers and publishes profile-aware eight-plus-four assets", () => {
  const publish = job("publish");
  const completenessIndex = publish.indexOf("Verify artifact completeness");
  const checksumIndex = publish.indexOf("Generate SHA-256 checksums");
  assert.ok(completenessIndex >= 0, "missing artifact completeness gate");
  assert.ok(checksumIndex > completenessIndex, "checksums run before completeness gate");
  for (const extension of ["exe", "dmg", "deb", "rpm"]) {
    assert.match(publish, new RegExp(`\\*\\.${extension}`));
  }
  assert.match(publish, /macos-aarch64/);
  assert.match(publish, /macos-x64/);
  assert.match(publish, /artifacts\/linux-x64-release/);
  assert.match(publish, /artifacts\/linux-arm64-release/);
  assert.match(publish, /APP_VERSION:\s*\$\{\{ needs\.validate-release\.outputs\.app_version \}\}/);
  assert.match(publish, /RELEASE_VERSION:\s*\$\{\{ needs\.validate-release\.outputs\.release_version \}\}/);
  assert.match(publish, /\*\$\{APP_VERSION\}\*_aarch64\.dmg/);
  assert.match(publish, /\*\$\{APP_VERSION\}\*_x64\.dmg/);
  assert.match(publish, /suyo-\$\{RELEASE_VERSION\}-windows-x64-setup\$\{INSTALLER_TRUST_SUFFIX\}\.exe/);
  assert.match(publish, /suyo-\$\{RELEASE_VERSION\}-windows-arm64-setup\$\{INSTALLER_TRUST_SUFFIX\}\.exe/);
  assert.match(publish, /suyo-\$\{RELEASE_VERSION\}-macos-aarch64\$\{MACOS_TRUST_SUFFIX\}\.dmg/);
  assert.match(publish, /suyo-\$\{RELEASE_VERSION\}-macos-x64\$\{MACOS_TRUST_SUFFIX\}\.dmg/);
  assert.match(publish, /MACOS_TRUST_SUFFIX="-ADHOC-NOT-NOTARIZED"/);
  assert.match(publish, /INSTALLER_TRUST_SUFFIX="-UNSIGNED-DEGRADED"/);
  for (const profiledName of [
    "windows-x64-setup${INSTALLER_TRUST_SUFFIX}.exe",
    "windows-arm64-setup${INSTALLER_TRUST_SUFFIX}.exe",
    "linux-amd64${INSTALLER_TRUST_SUFFIX}.deb",
    "linux-x86_64${INSTALLER_TRUST_SUFFIX}.rpm",
    "linux-arm64${INSTALLER_TRUST_SUFFIX}.deb",
    "linux-aarch64${INSTALLER_TRUST_SUFFIX}.rpm",
  ]) {
    assert.match(publish, new RegExp(escapeRegExp(profiledName)));
  }
  assert.match(publish, /generate-checksums\.mjs release-assets CHECKSUMS\.md/);
  assert.match(publish, /wc -l \| tr -d ' '\)" == "8"/);

  const release = step(publish, "Prepare GitHub Release");
  assert.match(release, /name:[^\n]*UNSIGNED-DEGRADED/);
  assert.match(release, /draft:[^\n]*release_profile != 'unsigned-degraded'/);
  assert.match(release, /prerelease:[^\n]*release_profile == 'unsigned-degraded'[^\n]*release_channel == 'prerelease'/);
  assert.match(release, /make_latest:\s*false/);
  assert.match(release, /body_path:\s*RELEASE-BODY\.md/);
  assert.match(release, /files:[\s\S]*CHECKSUMS\.md/);
  assert.match(release, /files:[\s\S]*release-manifest\.json/);
  assert.match(release, /files:[\s\S]*env\.OFFICE_COMPATIBILITY_ASSET/);
  assert.match(release, /files:[\s\S]*env\.RELEASE_CAPABILITIES_ASSET/);
  assert.match(release, /files:[\s\S]*release-assets\/\*/);
  const filesBlock = release.match(/files:\s*\|\n((?:\s{12}\S[^\n]*\n?)+)/)?.[1];
  assert.ok(filesBlock, "release files block is missing");
  assert.deepEqual(
    filesBlock.trim().split(/\r?\n/u).map((line) => line.trim()),
    [
      "release-assets/*",
      "CHECKSUMS.md",
      "release-manifest.json",
      "${{ env.OFFICE_COMPATIBILITY_ASSET }}",
      "${{ env.RELEASE_CAPABILITIES_ASSET }}",
    ],
  );

  const trust = step(publish, "Record installer trust status");
  assert.match(trust, /Developer ID/);
  assert.match(trust, /Apple 公证/);
  assert.match(trust, /ad-hoc 临时签名/);
  assert.match(trust, /未使用 Developer ID 签名/);
  assert.match(trust, /未经过 Apple 公证/);
  assert.match(trust, /Gatekeeper 可能拦截/);
  assert.match(trust, /先卸载候选版，或显式执行覆盖安装/);
  assert.match(trust, /Windows NSIS[^\n]*未配置 Authenticode/);
  assert.match(trust, /Linux x64\/ARM64 DEB\/RPM[^\n]*未配置仓库签名/);
  assert.match(trust, /十二个资产/);
  assert.match(trust, /八个安装包和四个元数据/);
  assert.match(trust, /OFFICE-RESTRICTED-COMPATIBILITY\.json/);
  assert.match(trust, /DEGRADED-CAPABILITIES\.json/);
  assert.match(trust, /release-manifest\.json[^\n]*手动下载/);
  assert.match(trust, /cat CHECKSUMS\.md/);
});

test("keeps stable Apple trust fail-closed while RC stays explicitly ad-hoc", () => {
  const mac = job("build-macos");
  const importCertificate = step(mac, "Import Apple certificate and discover signing identity");
  const signApp = step(mac, "Sign nested Mach-O files and app");
  const signDmg = step(mac, "Sign final DMG");
  const notarize = step(mac, "Notarize and staple final DMG");
  const gatekeeper = step(mac, "Verify official signatures and Gatekeeper");
  const rcBoundary = step(mac, "Verify RC ad-hoc trust boundary");
  const cleanup = step(mac, "Remove temporary Apple keychain");

  for (const stableOnly of [importCertificate, signDmg, notarize, gatekeeper, cleanup]) {
    assert.match(stableOnly, /needs\.validate-release\.outputs\.is_stable == 'true'/);
  }
  assert.match(signApp, /if \[\[ "\$IS_STABLE_RELEASE" == "true" \]\]/);
  assert.match(signApp, /SIGNING_IDENTITY="\$DEVELOPER_IDENTITY"/);
  assert.match(signApp, /SIGNING_IDENTITY="-"/);
  assert.match(signApp, /scripts\/sign-macos-app\.sh "\$APP_PATH" "\$SIGNING_IDENTITY"/);
  assert.match(signApp, /grep -Fxq "Signature=adhoc"/);
  assert.match(signApp, /grep -Fxq "Authority=\$SIGNING_IDENTITY"/);
  assert.match(rcBoundary, /is_stable == 'false'/);
  assert.match(rcBoundary, /unexpectedly has a code signature/);
  assert.match(rcBoundary, /unexpectedly has a stapled notarization ticket/);
});

test("repairs then signs inside-out and notarizes the final DMG", () => {
  const mac = job("build-macos");
  const repair = mac.indexOf("Repair Python.framework symlinks");
  const verify = mac.indexOf("Verify repaired app before signing");
  const importCertificate = mac.indexOf("Import Apple certificate and discover signing identity");
  const sign = mac.indexOf("Sign nested Mach-O files and app");
  const verifySigned = mac.indexOf("Verify signed app runtime");
  const createDmg = mac.indexOf("Create final DMG");
  const notarize = mac.indexOf("Notarize and staple final DMG");
  const signStep = step(mac, "Sign nested Mach-O files and app");
  const verifySignedStep = step(mac, "Verify signed app runtime");
  const createDmgStep = step(mac, "Create final DMG");
  const finalDmgStep = step(mac, "Verify final DMG contents");

  assert.ok(repair >= 0 && sign > repair, "signing must follow framework repair");
  assert.ok(verify > repair, "the repaired app must be verified before secrets are imported");
  assert.ok(
    importCertificate > verify && sign > importCertificate,
    "the Apple certificate must only be imported after build verification and immediately before signing",
  );
  assert.ok(verifySigned > sign, "signed app runtime must be verified after signing");
  assert.ok(createDmg > verifySigned, "DMG must be created after signed runtime verification");
  assert.ok(notarize > createDmg, "the final DMG must be the notarized object");
  assert.match(mac, /security find-identity/);
  assert.match(mac, /\^Developer ID Application:/);
  assert.doesNotMatch(mac, /wangzhang wu|46KF5Z549N/);
  assert.match(macSignScript, /com\.apple\.security\.cs\.allow-jit/);
  assert.match(macSignScript, /com\.apple\.security\.cs\.allow-unsigned-executable-memory/);
  for (const entitlement of [
    "com.apple.security.get-task-allow",
    "com.apple.security.cs.allow-dyld-environment-variables",
    "com.apple.security.cs.disable-executable-page-protection",
    "com.apple.security.cs.disable-library-validation",
  ]) {
    assert.match(
      macSignScript,
      new RegExp(`${escapeRegExp(entitlement)}[\\s\\S]*grep -q`),
    );
  }
  assert.match(macSignScript, /desktop-tauri\/src-tauri\/entitlements\.node\.plist/);
  assert.match(
    backendAdHocEntitlements,
    /<key>com\.apple\.security\.cs\.disable-library-validation<\/key>\s*<true\/>/,
  );
  assert.equal((backendAdHocEntitlements.match(/<true\/>/g) ?? []).length, 1);
  assert.equal((backendAdHocEntitlements.match(/<key>/g) ?? []).length, 1);
  for (const entitlement of [
    "com.apple.security.get-task-allow",
    "com.apple.security.cs.allow-dyld-environment-variables",
    "com.apple.security.cs.disable-executable-page-protection",
    "com.apple.security.cs.allow-jit",
    "com.apple.security.cs.allow-unsigned-executable-memory",
  ]) {
    assert.doesNotMatch(backendAdHocEntitlements, new RegExp(escapeRegExp(entitlement)));
  }
  assert.match(nodeEntitlements, /com\.apple\.security\.cs\.allow-jit/);
  assert.match(nodeEntitlements, /com\.apple\.security\.cs\.allow-unsigned-executable-memory/);
  assert.equal((nodeEntitlements.match(/<true\/>/g) ?? []).length, 2);
  for (const entitlement of [
    "com.apple.security.get-task-allow",
    "com.apple.security.cs.allow-dyld-environment-variables",
    "com.apple.security.cs.disable-executable-page-protection",
    "com.apple.security.cs.disable-library-validation",
  ]) {
    assert.doesNotMatch(nodeEntitlements, new RegExp(escapeRegExp(entitlement)));
  }
  assert.doesNotMatch(macSignScript, /codesign -d --entitlements :-/);
  assert.doesNotMatch(macSignScript, /plutil -(?:extract|remove) com\.apple\.security/);
  assert.doesNotMatch(macSignScript, /codesign --force[^\n]*--deep/);
  assert.match(
    macSignScript,
    /BACKEND_BINARY="\$APP_PATH\/Contents\/Resources\/backend\/suxiaoyou-backend"/,
  );
  assert.match(
    macSignScript,
    /BACKEND_ADHOC_ENTITLEMENTS="\$REPOSITORY_ROOT\/desktop-tauri\/src-tauri\/entitlements\.backend-adhoc\.plist"/,
  );
  assert.match(
    macSignScript,
    /elif \[\[ "\$candidate" == "\$BACKEND_BINARY" \]\]; then\s+if \[\[ "\$SIGNING_IDENTITY" == "-" \]\]; then\s+codesign "\$\{SIGN_ARGS\[@\]\}" --identifier "\$BACKEND_IDENTIFIER"[\s\S]*--entitlements "\$BACKEND_ADHOC_ENTITLEMENTS" "\$candidate"\s+else\s+codesign "\$\{SIGN_ARGS\[@\]\}" --identifier "\$BACKEND_IDENTIFIER" "\$candidate"/,
  );
  assert.equal(
    (macSignScript.match(/--entitlements "\$BACKEND_ADHOC_ENTITLEMENTS"/g) ?? []).length,
    1,
    "backend library-validation entitlement must only be applied by the ad-hoc branch",
  );
  assert.match(
    macSignScript,
    /if \[\[ "\$SIGNING_IDENTITY" == "-" \]\]; then[\s\S]*grep -q "com\.apple\.security\.cs\.disable-library-validation"[\s\S]*elif grep -q "com\.apple\.security\.cs\.disable-library-validation"[\s\S]*Developer ID backend must not disable library validation/,
  );
  assert.match(
    macSignScript,
    /codesign -dv --verbose=4 "\$BACKEND_BINARY" > "\$BACKEND_SIGNATURE_DETAILS" 2>&1\s+grep -Fxq "Identifier=\$BACKEND_IDENTIFIER" "\$BACKEND_SIGNATURE_DETAILS"/,
  );
  assert.match(verifySignedStep, /verify-macos-bundle\.mjs[^\n]*--verify-signature/);
  assert.doesNotMatch(verifySignedStep, /--skip-backend-smoke|VERIFY_BUNDLE_SKIP_SMOKE/);
  assert.match(createDmgStep, /hdiutil create[^\n]*-fs APFS[^\n]*-format UDZO/);
  assert.match(
    finalDmgStep,
    /ditto "\$MOUNT_DIRECTORY\/苏小有\.app" "\$INSTALL_TEST_DIRECTORY\/苏小有\.app"/,
  );
  assert.match(
    finalDmgStep,
    /verify-macos-bundle\.mjs "\$MOUNT_DIRECTORY\/苏小有\.app"[^\n]*--verify-signature/,
  );
  assert.match(
    finalDmgStep,
    /verify-macos-bundle\.mjs "\$INSTALL_TEST_DIRECTORY\/苏小有\.app"[^\n]*--verify-signature/,
  );
  assert.match(mac, /notarytool submit "\$DMG_PATH"/);
  assert.match(mac, /stapler staple "\$DMG_PATH"/);
  assert.match(mac, /stapler validate "\$DMG_PATH"/);
  assert.match(macSignScript, /codesign --verify --deep --strict/);
  assert.match(macSignScript, /OFFICE_RENDERER_ROOT=/);
  assert.match(macSignScript, /verify_presigned_renderer_code/);
  assert.match(
    macSignScript,
    /"\$candidate" == "\$OFFICE_RENDERER_ROOT\/"\*[\s\S]*verify_presigned_renderer_code "\$candidate"/,
  );
  assert.match(mac, /SIGNING_IDENTITY="-"/);
  assert.match(macSignScript, /SIGN_ARGS=\(--force --options runtime --sign -\)/);
  assert.match(mac, /--verify-signature/);
  assert.match(mac, /spctl --assess/);
  assert.match(mac, /hdiutil verify/);
});

test("retries only transient hdiutil Resource busy failures when creating the DMG", () => {
  const createDmg = step(job("build-macos"), "Create final DMG");
  const createIndex = createDmg.indexOf("hdiutil create");
  const verifyIndex = createDmg.indexOf('hdiutil verify "$DMG_PATH"');

  assert.match(createDmg, /MAX_HDIUTIL_ATTEMPTS=3/);
  assert.match(
    createDmg,
    /for \(\( attempt = 1; attempt <= MAX_HDIUTIL_ATTEMPTS; attempt\+\+ \)\); do/,
  );
  assert.equal((createDmg.match(/hdiutil create -volname/g) ?? []).length, 1);
  assert.match(
    createDmg,
    /rm -f "\$DMG_PATH"[\s\S]*hdiutil_output="\$\([\s\S]*hdiutil create/,
  );
  assert.match(createDmg, /hdiutil_status=\$\?/);
  assert.match(
    createDmg,
    /hdiutil_status != 1[\s\S]*grep -Fqx "hdiutil: create failed - Resource busy"/,
  );
  assert.match(
    createDmg,
    /non-transient error \(status \$hdiutil_status\)"\s+exit "\$hdiutil_status"/,
  );
  assert.match(
    createDmg,
    /attempt == MAX_HDIUTIL_ATTEMPTS[\s\S]*remained busy[\s\S]*exit "\$hdiutil_status"/,
  );
  assert.match(createDmg, /retry_delay=\$\(\( attempt \* 5 \)\)/);
  assert.match(createDmg, /sleep "\$retry_delay"/);
  assert.match(createDmg, /\[\[ -s "\$DMG_PATH" \]\]/);
  assert.doesNotMatch(createDmg, /hdiutil create[^\n]*(?:\|\| true|; true)/);
  assert.ok(createIndex >= 0 && verifyIndex > createIndex);
});

test("CI gates production builds, audits, bundle smoke, and test suites", () => {
  assert.match(ciWorkflow, /node --test tests\/unit\/\*\.test\.ts/);
  assert.match(ciWorkflow, /node --test scripts\/\*\.test\.mjs/);
  assert.match(ciWorkflow, /release-metadata\.mjs/);
  assert.match(ciWorkflow, /pip-audit==2\.10\.1 uv==0\.11\.28/);
  assert.match(ciWorkflow, /pip-audit -r requirements\.txt/);
  assert.match(ciWorkflow, /npm audit --omit=dev/);
  assert.match(ciWorkflow, /npm --prefix frontend audit --omit=dev/);
  assert.match(ciWorkflow, /npm --prefix desktop-tauri audit --omit=dev/);
  assert.match(ciWorkflow, /npm run build:frontend/);
  for (const target of [
    "x86_64-pc-windows-msvc",
    "aarch64-apple-darwin",
    "x86_64-apple-darwin",
    "aarch64-manylinux_2_28",
    "x86_64-manylinux_2_28",
  ]) {
    assert.match(ciWorkflow, new RegExp(escapeRegExp(target)));
  }
  assert.match(
    ciWorkflow,
    /uv pip install[\s\S]*--system[\s\S]*--require-hashes[\s\S]*--only-binary=:all:/,
  );
  assert.match(ciWorkflow, /cargo test --locked/);
  assert.match(ciWorkflow, /cargo clippy --locked --all-targets -- -D warnings/);
  assert.match(ciWorkflow, /cargo install cargo-audit --locked --version 0\.22\.2/);
  assert.match(ciWorkflow, /cargo audit --file Cargo\.lock/);
  assert.match(ciWorkflow, /cargo build --release --locked/);

  const windowsAudit = ciJob("backend-windows-audit");
  assert.match(windowsAudit, /runs-on:\s*windows-latest/);
  assert.match(windowsAudit, /python-version:\s*"3\.12\.10"/);
  assert.match(windowsAudit, /python -m pip install pip-audit==2\.10\.1/);
  assert.match(
    step(windowsAudit, "Audit Windows backend production dependency graph"),
    /python -m pip_audit --strict --require-hashes -r backend\/requirements\.txt/,
  );

  const packaging = ciJob("packaging-smoke");
  assert.match(packaging, /SUXIAOYOU_OFFICE_RENDERER_PROFILE:\s*unsigned-degraded/);
  assert.match(packaging, /VERIFY_BUNDLE_V11_GATE_MODE:\s*released/);
  assert.match(packaging, /VERIFY_BUNDLE_OFFICE_RENDERER_PROFILE:\s*unsigned-degraded/);
  assert.match(packaging, /VERIFY_BUNDLE_OFFICE_PLATFORM:\s*linux-x64/);
  assert.match(packaging, /python -m PyInstaller suxiaoyou\.spec --noconfirm/);
  assert.match(packaging, /node scripts\/verify-bundle\.mjs backend\/dist\/suxiaoyou-backend/);
  assert.match(packaging, /bubblewrap/);
  assert.match(packaging, /npm exec tauri build -- --config src-tauri\/build\.linux-x64\.json --bundles deb --no-sign/);
  assert.match(packaging, /dpkg-deb -x/);
  assert.match(packaging, /node scripts\/verify-node-runtime\.mjs/);
  assert.match(packaging, /node scripts\/verify-bundle\.mjs "\$\(dirname/);
  assert.match(packaging, /grep -Eq '\(\^\|, \)bubblewrap/);
  assert.match(packaging, /sudo dpkg -i "\$DEB"/);
  assert.match(packaging, /dpkg-query -W/);
  assert.match(packaging, /sudo dpkg --purge "\$DEB_PACKAGE"/);
  assert.match(ciWorkflow, /playwright install --with-deps chromium/);
  assert.match(ciWorkflow, /npm run test:ui:core/);
});
