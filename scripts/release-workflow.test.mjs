import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const workflow = readFileSync(join(root, ".github/workflows/release.yml"), "utf8");
const ciWorkflow = readFileSync(join(root, ".github/workflows/ci.yml"), "utf8");
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
  assert.match(publish, /if:\s*github\.event_name == 'push'/);
  assert.match(publish, /startsWith\(github\.ref, 'refs\/tags\/'\)/);

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

test("manual releases select one target while tags keep both native macOS builds", () => {
  const mac = job("build-macos");
  for (const target of [
    "macos-aarch64",
    "macos-x64",
    "windows-x64",
    "linux-x64",
    "linux-arm64",
  ]) {
    assert.match(workflow, new RegExp(`^\\s{10}- ${escapeRegExp(target)}$`, "m"));
  }
  assert.match(workflow, /^\s{8}default:\s*macos-aarch64$/m);
  assert.match(job("build-windows"), /inputs\.target == 'windows-x64'/);
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

test("installer artifacts expire after one day and lifecycle diagnostics after seven", () => {
  const jobs = [
    [
      "build-windows",
      "Upload Windows artifact",
      "Upload Windows lifecycle diagnostics",
      "suxiaoyou-desktop-lifecycle-windows",
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
    assert.match(step(build, installerStep), /if:\s*always\(\)/);
    assert.match(step(build, installerStep), /retention-days:\s*1/);
    const diagnostics = step(build, diagnosticsStep);
    assert.match(diagnostics, /if:\s*always\(\)/);
    assert.match(diagnostics, /if-no-files-found:\s*ignore/);
    assert.match(diagnostics, /retention-days:\s*7/);
    assert.match(diagnostics, new RegExp(escapeRegExp(diagnosticPath)));
    assert.equal(diagnosticPaths.has(diagnosticPath), false, diagnosticPath);
    diagnosticPaths.add(diagnosticPath);
  }
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
  assert.deepEqual(linuxConfig.bundle.linux.deb.depends, ["libxdo3"]);
  assert.deepEqual(linuxConfig.bundle.linux.rpm.depends, ["xdotool"]);
  assert.deepEqual(linuxArmConfig.bundle.linux.deb.depends, ["libxdo3"]);
  assert.deepEqual(linuxArmConfig.bundle.linux.rpm.depends, ["xdotool"]);
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
  assert.ok(nsisTemplate.includes('Name "$(SuyoDisplayName)"'));
  assert.ok(nsisTemplate.includes('VIAddVersionKey "ProductName" "suyo"'));
  assert.ok(nsisTemplate.includes('VIAddVersionKey "FileDescription" "suyo"'));
  assert.equal(
    nsisTemplate.match(
      /CheckIfAppIsRunning "\$\{MAINBINARYNAME\}\.exe" "\$\(SuyoDisplayName\)"/g,
    )?.length,
    2,
  );
  assert.ok(nsisTemplate.includes('"Open with $(SuyoDisplayName)"'));
  assert.ok(
    nsisTemplate.includes(
      'WriteRegStr SHCTX "${UNINSTKEY}" "DisplayName" "$(SuyoDisplayName)"',
    ),
  );
  assert.ok(
    nsisTemplate.includes(
      'CreateShortcut "$SMPROGRAMS\\$(SuyoDisplayName).lnk" "$INSTDIR\\${MAINBINARYNAME}.exe"',
    ),
  );
  assert.ok(
    nsisTemplate.includes(
      'CreateShortcut "$DESKTOP\\$(SuyoDisplayName).lnk" "$INSTDIR\\${MAINBINARYNAME}.exe"',
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
    /七个安装包（Windows x64、macOS arm64\/x64、Linux x64\/ARM64）均来自同一提交/,
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

  const metadata = step(validate, "Validate metadata and workflow contracts");
  assert.match(metadata, /APP_VERSION:\s*\$\{\{ steps\.release-context\.outputs\.app_version \}\}/);
  assert.match(metadata, /release-metadata\.mjs "\$APP_VERSION"/);
  assert.doesNotMatch(metadata, /GITHUB_REF_NAME#v/);
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
  assert.match(workflow, /git rev-parse "\$GITHUB_SHA\^\{commit\}"/);
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
    "always-run artifact upload must follow installed-content verification",
  );
  assert.match(step(linux, "Upload Linux artifacts"), /if:\s*always\(\)/);
  assert.match(linux, /dpkg-deb -x/);
  assert.match(linux, /DEB_PACKAGE=.*dpkg-deb -f .* Package/);
  assert.match(linux, /dpkg-deb -f .* Version/);
  assert.match(linux, /dpkg-deb -f .* Architecture/);
  assert.match(linux, /DEB_DEPENDS=.*dpkg-deb -f .* Depends/);
  assert.match(linux, /libxdo3\(\[, \(\]\|\$\)/);
  assert.match(linux, /RPM_PACKAGE=.*rpm -qp --queryformat '%\{NAME\}'/);
  assert.match(linux, /RPM_VERSION=.*rpm -qp --queryformat '%\{VERSION\}'/);
  assert.match(linux, /RPM_ARCH=.*rpm -qp --queryformat '%\{ARCH\}'/);
  assert.match(linux, /RPM_REQUIRES=.*rpm -qp --requires/);
  assert.match(linux, /xdotool/);
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
  const install = step(
    windows,
    "Install NSIS package and verify packaged Node.js toolchain",
  );
  assert.match(install, /Start-Process/);
  assert.match(install, /"\/S"/);
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
  assert.match(install, /CreateShortcut\(\$legacyShortcut\)/);
  assert.match(install, /Localized shortcut is missing/);
  assert.match(install, /Legacy Chinese shortcut was not migrated/);
  assert.match(desktopCargo, /^\[package\]\nname = "suxiaoyou-desktop"$/m);
  assert.equal(tauriConfig.mainBinaryName, undefined);
  assert.match(install, /id:\s*install-windows/);
  assert.match(install, /expectedAppExecutable = "suxiaoyou-desktop\.exe"/);
  assert.match(install, /expectedAppPath = Join-Path \$installDirectory \$expectedAppExecutable/);
  assert.match(install, /appBinary = \$expectedAppPath/);
  assert.match(install, /Test-Path -LiteralPath \$appBinary -PathType Leaf/);
  assert.match(install, /VersionInfo\.ProductVersion/);
  assert.match(install, /Filter node\.exe/);
  assert.match(install, /node scripts\/verify-node-runtime\.mjs/);
  assert.match(install, /npm\.cmd/);
  assert.match(install, /npx\.cmd/);
  assert.match(install, /suxiaoyou-backend\.exe/);
  assert.match(install, /node scripts\/verify-bundle\.mjs/);
  assert.match(
    install,
    /"installed_app=\$appBinary" \| Out-File[\s\S]*-FilePath \$env:GITHUB_OUTPUT/,
  );
  assert.doesNotMatch(install, /苏小有\.exe/);
});

test("launches every installed desktop, waits for backend ready, and proves clean exit", () => {
  const windows = step(
    job("build-windows"),
    "Launch installed Windows desktop and verify clean shutdown",
  );
  assert.match(
    windows,
    /WINDOWS_INSTALLED_APP:\s*\$\{\{ steps\.install-windows\.outputs\.installed_app \}\}/,
  );
  assert.match(windows, /Test-Path -LiteralPath \$installedApp -PathType Leaf/);
  assert.match(windows, /--executable \$installedApp/);
  assert.doesNotMatch(windows, /Get-ChildItem|苏小有\.exe/);
  assert.match(windows, /verify-desktop-lifecycle\.mjs/);
  assert.match(windows, /suxiaoyou-desktop-lifecycle-windows/);

  const mac = step(job("build-macos"), "Verify final DMG contents");
  const copiedApp = mac.indexOf("ditto \"$MOUNT_DIRECTORY/苏小有.app\"");
  const lifecycle = mac.indexOf("verify-desktop-lifecycle.mjs");
  assert.ok(copiedApp >= 0 && lifecycle > copiedApp);
  assert.match(mac, /CFBundleExecutable/);
  assert.match(mac, /suxiaoyou-desktop-lifecycle-macos-\$DMG_ARCH/);

  const linux = step(job("build-linux"), "Install Linux packages and verify desktop lifecycle");
  assert.match(linux, /sudo dpkg -i/);
  assert.match(linux, /sudo rpm -i --nodeps/);
  assert.match(linux, /xvfb-run -a dbus-run-session/);
  assert.equal((linux.match(/verify-desktop-lifecycle\.mjs/g) ?? []).length, 2);
  assert.match(linux, /sudo dpkg --purge/);
  assert.match(linux, /sudo rpm -e/);

  for (const name of ["build-windows", "build-macos", "build-linux"]) {
    assert.match(job(name), /lifecycle-diagnostics/);
    assert.match(job(name), /if-no-files-found:\s*ignore/);
  }
});

test("Windows native build validates lifecycle primitives before packaging", () => {
  const windows = job("build-windows");
  const buildBackend = step(windows, "Build backend with locked PyInstaller");

  assert.match(buildBackend, /pytest==9\.1\.1/);
  assert.match(buildBackend, /python -m pytest -q backend\/tests\/test_run\.py/);
  assert.match(buildBackend, /backend\/tests\/test_scripts\/test_download_node\.py/);
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

test("validates release metadata, production audits, and tests before building", () => {
  const validate = job("validate-release");
  const install = step(validate, "Install validation dependencies");
  const backend = step(validate, "Audit and test backend production graph");

  assert.match(
    install,
    /python -m pip install --require-hashes --only-binary=cryptography -r backend\/requirements\.txt/,
  );
  assert.match(install, /python -m pip install -e \.\/backend --no-deps/);
  assert.match(
    install,
    /python -m pip install pytest==9\.1\.1 pytest-asyncio==1\.4\.0 pip-audit==2\.10\.1/,
  );
  assert.doesNotMatch(install, /backend\[(?:dev|mcp)/);
  assert.match(validate, /node scripts\/release-metadata\.mjs/);
  assert.match(validate, /node --test scripts\/\*\.test\.mjs/);
  assert.match(validate, /npm audit --omit=dev/);
  assert.match(validate, /node --test tests\/unit\/\*\.test\.ts/);
  assert.match(validate, /playwright install --with-deps chromium/);
  assert.match(validate, /npm run test:ui:core/);
  assert.match(backend, /python -m pip check/);
  assert.match(backend, /pip-audit -r requirements\.txt/);
  assert.match(backend, /pytest -q/);
  assert.match(validate, /cargo metadata --locked --format-version 1/);
  assert.match(validate, /cargo test --locked/);
  assert.match(validate, /cargo clippy --locked --all-targets -- -D warnings/);

  for (const name of ["build-windows", "build-macos", "build-linux"]) {
    assert.match(job(name), /needs:\s*validate-release/);
    assert.match(
      job(name),
      /python -m pip install --require-hashes --only-binary=cryptography -r backend\/requirements\.txt/,
    );
    assert.match(job(name), /python -m pip install pyinstaller==6\.21\.0/);
    assert.doesNotMatch(
      job(name),
      /requirements\.txt pyinstaller==/,
      "hash-locked runtime dependencies and PyInstaller must be separate pip invocations",
    );
  }
});

test("pins the packaged backend graph and excludes unreachable native SDKs", () => {
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
  ]) {
    assert.match(backendProject, new RegExp(`"${escapeRegExp(requirement)}"`));
  }
  assert.doesNotMatch(
    `${backendProject}\n${backendRequirements}`,
    /xhtml2pdf|python-bidi|svglib|pyHanko|pyhanko-certvalidator/i,
  );
  assert.doesNotMatch(backendProject, /"(?:anthropic|google-genai)[^"\n]*"/);
  assert.doesNotMatch(backendProject, /^mcp\s*=\s*\[/m);
});

test("verifies all seven installers before publishing checksums", () => {
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
  assert.match(publish, /suyo-\$\{RELEASE_VERSION\}-windows-x64-setup\.exe/);
  assert.match(publish, /suyo-\$\{RELEASE_VERSION\}-macos-aarch64\$\{MACOS_TRUST_SUFFIX\}\.dmg/);
  assert.match(publish, /suyo-\$\{RELEASE_VERSION\}-macos-x64\$\{MACOS_TRUST_SUFFIX\}\.dmg/);
  assert.match(publish, /MACOS_TRUST_SUFFIX="-ADHOC-NOT-NOTARIZED"/);
  for (const stableName of [
    "windows-x64-setup.exe",
    "linux-amd64.deb",
    "linux-x86_64.rpm",
    "linux-arm64.deb",
    "linux-aarch64.rpm",
  ]) {
    assert.match(publish, new RegExp(escapeRegExp(stableName)));
  }
  assert.match(publish, /generate-checksums\.mjs release-assets CHECKSUMS\.md/);
  assert.match(publish, /wc -l \| tr -d ' '\)" == "7"/);

  const release = step(publish, "Prepare GitHub Release");
  assert.match(release, /draft:\s*true/);
  assert.match(release, /prerelease:\s*\$\{\{ needs\.validate-release\.outputs\.release_channel == 'prerelease' \}\}/);
  assert.match(release, /make_latest:\s*false/);
  assert.match(release, /body_path:\s*RELEASE-BODY\.md/);
  assert.match(release, /files:[\s\S]*CHECKSUMS\.md/);
  assert.match(release, /files:[\s\S]*release-manifest\.json/);
  assert.match(release, /files:[\s\S]*release-assets\/\*/);

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
  assert.match(mac, /com\.apple\.security\.cs\.allow-jit/);
  assert.match(mac, /com\.apple\.security\.cs\.allow-unsigned-executable-memory/);
  for (const entitlement of [
    "com.apple.security.get-task-allow",
    "com.apple.security.cs.allow-dyld-environment-variables",
    "com.apple.security.cs.disable-executable-page-protection",
    "com.apple.security.cs.disable-library-validation",
  ]) {
    assert.match(mac, new RegExp(`${escapeRegExp(entitlement)}[\\s\\S]*grep -q`));
  }
  assert.match(mac, /desktop-tauri\/src-tauri\/entitlements\.node\.plist/);
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
  assert.doesNotMatch(mac, /codesign -d --entitlements :-/);
  assert.doesNotMatch(mac, /plutil -(?:extract|remove) com\.apple\.security/);
  assert.doesNotMatch(mac, /codesign --force[^\n]*--deep/);
  assert.match(
    signStep,
    /BACKEND_BINARY="\$APP_PATH\/Contents\/Resources\/backend\/suxiaoyou-backend"/,
  );
  assert.match(
    signStep,
    /BACKEND_ADHOC_ENTITLEMENTS="desktop-tauri\/src-tauri\/entitlements\.backend-adhoc\.plist"/,
  );
  assert.match(
    signStep,
    /elif \[\[ "\$SIGNING_IDENTITY" == "-" && "\$candidate" == "\$BACKEND_BINARY" \]\]; then\s+codesign "\$\{SIGN_ARGS\[@\]\}" --entitlements "\$BACKEND_ADHOC_ENTITLEMENTS" "\$candidate"/,
  );
  assert.equal(
    (signStep.match(/--entitlements "\$BACKEND_ADHOC_ENTITLEMENTS"/g) ?? []).length,
    1,
    "backend library-validation entitlement must only be applied by the ad-hoc branch",
  );
  assert.match(
    signStep,
    /if \[\[ "\$SIGNING_IDENTITY" == "-" \]\]; then[\s\S]*grep -q "com\.apple\.security\.cs\.disable-library-validation"[\s\S]*elif grep -q "com\.apple\.security\.cs\.disable-library-validation"[\s\S]*Developer ID backend must not disable library validation/,
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
  assert.match(mac, /codesign --verify --deep --strict/);
  assert.match(mac, /SIGNING_IDENTITY="-"/);
  assert.match(mac, /SIGN_ARGS=\(--force --options runtime --sign -\)/);
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

test("CI runs the frontend unit suite", () => {
  assert.match(ciWorkflow, /node --test tests\/unit\/\*\.test\.ts/);
  assert.match(ciWorkflow, /release-workflow\.test\.mjs/);
  assert.match(ciWorkflow, /verify-macos-bundle\.test\.mjs/);
  assert.match(ciWorkflow, /verify-node-runtime\.test\.mjs/);
  assert.match(ciWorkflow, /workflow-security\.test\.mjs/);
  assert.match(ciWorkflow, /bump-version\.test\.mjs/);
  assert.match(ciWorkflow, /release-manifest\.test\.mjs/);
  assert.match(ciWorkflow, /release-metadata\.mjs/);
  assert.match(ciWorkflow, /verify-desktop-lifecycle\.test\.mjs/);
  assert.match(ciWorkflow, /python -m pip install uv==0\.11\.28/);
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
  assert.match(ciWorkflow, /playwright install --with-deps chromium/);
  assert.match(ciWorkflow, /npm run test:ui:core/);
});
