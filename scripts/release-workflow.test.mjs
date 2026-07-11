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
const macArmConfig = JSON.parse(
  readFileSync(join(root, "desktop-tauri/src-tauri/build.macos-aarch64.json"), "utf8"),
);
const macIntelConfig = JSON.parse(
  readFileSync(join(root, "desktop-tauri/src-tauri/build.macos-x64.json"), "utf8"),
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
  assert.match(mac, /ADHOC-TEST/);

  const validate = job("validate-release");
  assert.match(validate, /fetch-depth:\s*0/);
  assert.match(validate, /Require official tag commit on main/);
  assert.match(validate, /git merge-base --is-ancestor "\$GITHUB_SHA" refs\/remotes\/origin\/main/);
});

test("manual releases select one target while tags keep both native macOS builds", () => {
  const mac = job("build-macos");
  for (const target of [
    "macos-aarch64",
    "macos-x64",
    "windows-x64",
    "linux-x64",
  ]) {
    assert.match(workflow, new RegExp(`^\\s{10}- ${escapeRegExp(target)}$`, "m"));
  }
  assert.match(workflow, /^\s{8}default:\s*macos-aarch64$/m);
  assert.match(job("build-windows"), /inputs\.target == 'windows-x64'/);
  assert.match(job("build-linux"), /inputs\.target == 'linux-x64'/);
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
  assert.deepEqual(macArmConfig.bundle.resources, tauriConfig.bundle.resources);
  assert.deepEqual(macIntelConfig.bundle.resources, tauriConfig.bundle.resources);
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

test("keeps Apple credentials step-scoped and fails official tags fast", () => {
  const beforeJobs = workflow.slice(0, workflow.indexOf("\njobs:\n"));
  assert.doesNotMatch(beforeJobs, /APPLE_(?:CERTIFICATE|CERTIFICATE_PASSWORD|ID|PASSWORD|TEAM_ID)/);

  for (const line of workflow.split("\n")) {
    if (!line.includes("secrets.APPLE_")) continue;
    const indentation = line.match(/^\s*/)[0].length;
    assert.ok(indentation >= 10, `Apple secret is not step-scoped: ${line.trim()}`);
  }

  const validate = job("validate-release");
  const credentials = step(validate, "Require Apple credentials for an official tag");
  assert.match(credentials, /if:\s*github\.event_name == 'push'/);
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
  assert.match(linux, /dpkg-deb -f .* Version/);
  assert.match(linux, /dpkg-deb -f .* Architecture/);
  assert.match(linux, /RPM_VERSION=.*rpm -qp --queryformat '%\{VERSION\}'/);
  assert.match(linux, /RPM_ARCH=.*rpm -qp --queryformat '%\{ARCH\}'/);
  assert.match(linux, /expected amd64/);
  assert.match(linux, /expected x86_64/);
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
  assert.match(install, /Filter "苏小有\.exe"/);
  assert.match(install, /VersionInfo\.ProductVersion/);
  assert.match(install, /Filter node\.exe/);
  assert.match(install, /node scripts\/verify-node-runtime\.mjs/);
  assert.match(install, /npm\.cmd/);
  assert.match(install, /npx\.cmd/);
  assert.match(install, /suxiaoyou-backend\.exe/);
  assert.match(install, /node scripts\/verify-bundle\.mjs/);
});

test("launches every installed desktop, waits for backend ready, and proves clean exit", () => {
  const windows = step(
    job("build-windows"),
    "Launch installed Windows desktop and verify clean shutdown",
  );
  assert.match(windows, /苏小有\.exe/);
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

test("verifies all five installer types before publishing checksums", () => {
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
  assert.match(publish, /VERSION="\$\{GITHUB_REF_NAME#v\}"/);
  assert.match(publish, /\*\$\{VERSION\}\*_aarch64\.dmg/);
  assert.match(publish, /\*\$\{VERSION\}\*_x64\.dmg/);
  for (const stableName of [
    "windows-x64-setup.exe",
    "macos-aarch64.dmg",
    "macos-x64.dmg",
    "linux-amd64.deb",
    "linux-x86_64.rpm",
  ]) {
    assert.match(publish, new RegExp(escapeRegExp(stableName)));
  }
  assert.match(publish, /generate-checksums\.mjs release-assets CHECKSUMS\.md/);

  const release = step(publish, "Create draft GitHub Release");
  assert.match(release, /draft:\s*true/);
  assert.match(release, /body_path:\s*RELEASE-BODY\.md/);
  assert.match(release, /files:[\s\S]*CHECKSUMS\.md/);
  assert.match(release, /files:[\s\S]*release-manifest\.json/);
  assert.match(release, /files:[\s\S]*release-assets\/\*/);

  const trust = step(publish, "Record installer trust status");
  assert.match(trust, /Developer ID/);
  assert.match(trust, /Apple 公证/);
  assert.match(trust, /Windows NSIS[^\n]*未配置 Authenticode/);
  assert.match(trust, /Linux DEB\/RPM[^\n]*未配置仓库签名/);
  assert.match(trust, /release-manifest\.json[^\n]*手动下载/);
  assert.match(trust, /cat CHECKSUMS\.md/);
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
