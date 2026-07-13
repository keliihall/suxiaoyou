import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFileSync, readdirSync } from "node:fs";
import { dirname, join } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");

function read(relativePath) {
  return readFileSync(join(root, relativePath), "utf8");
}

const requiredThirdPartyFiles = [
  "ANTHROPIC-CANVAS-FONTS-OFL-1.1.txt",
  "ANTHROPIC-KNOWLEDGE-WORK-PLUGINS-APACHE-2.0.txt",
  "ANTHROPIC-SKILLS-APACHE-2.0.txt",
  "CDLA-PERMISSIVE-2.0.txt",
  "COLORAMA-0.4.6-LICENSE.txt",
  "CPYTHON-3.12.13-LICENSE.txt",
  "JAVASCRIPT-LICENSES.txt",
  "MOZILLA-PUBLIC-LICENSE-2.0.txt",
  "NANOBOT-MIT.txt",
  "NODEJS-22.22.0-LICENSE.txt",
  "OPENCLAW-MIT.txt",
  "PYINSTALLER-6.21.0-COPYING.txt",
  "PYTHON-LICENSES.txt",
  "PYWIN32-312-LICENSES.txt",
  "README.md",
  "RUST-LICENSES.html",
  "SHADCN-UI-MIT.txt",
  "SOURCE_AVAILABILITY.md",
  "SUXIAOYOU-CJK-FONT-OFL-1.1.txt",
  "TENCENT-WEIXIN-OPENCLAW-1.0.3-MIT.txt",
  "TQDM-4.68.4-LICENSE.txt",
  "WEBENCODINGS-0.5.1-BSD-3-CLAUSE.txt",
  "python-runtime",
];

const standaloneRuntimeLicenses = [
  "LICENSE.bdb.txt",
  "LICENSE.bzip2.txt",
  "LICENSE.cpython.txt",
  "LICENSE.expat.txt",
  "LICENSE.libX11.txt",
  "LICENSE.libXau.txt",
  "LICENSE.libedit.txt",
  "LICENSE.libffi.txt",
  "LICENSE.liblzma.txt",
  "LICENSE.libuuid.txt",
  "LICENSE.libxcb.txt",
  "LICENSE.mpdecimal.txt",
  "LICENSE.ncurses.txt",
  "LICENSE.openssl-1.1.txt",
  "LICENSE.openssl-3.txt",
  "LICENSE.sqlite.txt",
  "LICENSE.tcl.txt",
  "LICENSE.tix.txt",
  "LICENSE.zlib.txt",
];

const expectedResources = {
  "../../backend/dist/suxiaoyou-backend": "backend",
  "../../backend/resources/nodejs": "nodejs",
  "../../LICENSE": "licenses/LICENSE",
  "../../NOTICE": "licenses/NOTICE",
  "../../THIRD_PARTY_NOTICES.md": "licenses/THIRD_PARTY_NOTICES.md",
  "../../release-licenses": "licenses/third-party",
};

const tauriConfigs = [
  "desktop-tauri/src-tauri/tauri.conf.json",
  "desktop-tauri/src-tauri/build.windows.json",
  "desktop-tauri/src-tauri/build.macos-aarch64.json",
  "desktop-tauri/src-tauri/build.macos-x64.json",
  "desktop-tauri/src-tauri/build.linux-x64.json",
  "desktop-tauri/src-tauri/build.linux-arm64.json",
];

const macPlatformConfig = JSON.parse(
  read("desktop-tauri/src-tauri/tauri.macos.conf.json"),
);

test("all desktop platforms ship the required release-license resources", () => {
  const resourceMaps = tauriConfigs.map(
    (path) => JSON.parse(read(path)).bundle.resources,
  );

  for (const [index, resources] of resourceMaps.entries()) {
    for (const [source, destination] of Object.entries(expectedResources)) {
      assert.equal(resources[source], destination, `${tauriConfigs[index]}: ${source}`);
    }
  }

  for (const index of [0, 1, 2, 3, 4, 5]) {
    assert.deepEqual(resourceMaps[index], expectedResources, tauriConfigs[index]);
  }
  assert.equal(macPlatformConfig.bundle.targets[0], "dmg");
  assert.equal(macPlatformConfig.bundle.targets[1], "app");
  assert.deepEqual(macPlatformConfig.bundle.resources, {
    "macos-locales/en.lproj/InfoPlist.strings": "en.lproj/InfoPlist.strings",
    "macos-locales/zh-Hans.lproj/InfoPlist.strings": "zh-Hans.lproj/InfoPlist.strings",
  });
});

test("the checked-in static license baseline contains every required text", () => {
  assert.deepEqual(
    readdirSync(join(root, "release-licenses")).sort(),
    requiredThirdPartyFiles,
  );

  assert.match(read("LICENSE"), /Apache License\s+Version 2\.0/);
  assert.match(read("NOTICE"), /Copyright 2026 W Axis Inc\./);
  assert.match(
    read("release-licenses/NANOBOT-MIT.txt"),
    /Copyright \(c\) 2025-present Xubin Ren and the nanobot contributors/,
  );
  assert.match(
    read("release-licenses/OPENCLAW-MIT.txt"),
    /Copyright \(c\) 2025 Peter Steinberger/,
  );
  assert.match(
    read("release-licenses/TENCENT-WEIXIN-OPENCLAW-1.0.3-MIT.txt"),
    /Copyright \(c\) 2026 Tencent Inc\./,
  );
  assert.match(
    read("release-licenses/NODEJS-22.22.0-LICENSE.txt"),
    /Copyright Node\.js contributors\. All rights reserved\./,
  );
  assert.match(
    read("release-licenses/CPYTHON-3.12.13-LICENSE.txt"),
    /PYTHON SOFTWARE FOUNDATION LICENSE VERSION 2/,
  );
  assert.match(
    read("release-licenses/PYINSTALLER-6.21.0-COPYING.txt"),
    /Bootloader Exception[\s\S]*unlimited permission to link or embed compiled bootloader/,
  );
  assert.match(
    read("release-licenses/MOZILLA-PUBLIC-LICENSE-2.0.txt"),
    /Mozilla Public License Version 2\.0/,
  );
  assert.match(
    read("release-licenses/CDLA-PERMISSIVE-2.0.txt"),
    /Community Data License Agreement - Permissive - Version 2\.0/,
  );
  assert.match(
    read("release-licenses/TQDM-4.68.4-LICENSE.txt"),
    /MPL-2\.0 2015-2026 \(c\) Casper da Costa-Luis/,
  );
  assert.match(
    read("release-licenses/WEBENCODINGS-0.5.1-BSD-3-CLAUSE.txt"),
    /Copyright \(c\) 2012 by Simon Sapin\. Some rights reserved\.[\s\S]*Redistribution and use in source and binary forms/,
  );
  assert.match(
    read("release-licenses/ANTHROPIC-SKILLS-APACHE-2.0.txt"),
    /Copyright 2026 Anthropic, PBC\./,
  );
  assert.match(
    read("release-licenses/ANTHROPIC-KNOWLEDGE-WORK-PLUGINS-APACHE-2.0.txt"),
    /Apache License\s+Version 2\.0/,
  );
  const fontNotices = read("release-licenses/ANTHROPIC-CANVAS-FONTS-OFL-1.1.txt");
  assert.equal((fontNotices.match(/^## /gm) ?? []).length, 27);
  assert.match(fontNotices, /SIL OPEN FONT LICENSE Version 1\.1/);
  assert.match(
    read("release-licenses/SHADCN-UI-MIT.txt"),
    /Copyright \(c\) 2023 shadcn/,
  );
  const pdfFontLicense = read("release-licenses/SUXIAOYOU-CJK-FONT-OFL-1.1.txt");
  assert.match(pdfFontLicense, /\(c\) 2014-2021 Adobe/);
  assert.match(pdfFontLicense, /SIL OPEN FONT LICENSE Version 1\.1/);
  assert.match(
    pdfFontLicense,
    /c3e1564838ecaa70dcfb786e50670fb8cf3ac4e535584f01b0a00fe158931248/,
  );
  const pdfFont = readFileSync(
    join(root, "backend/app/data/fonts/SuxiaoyouCJK-Regular.ttf"),
  );
  assert.equal(
    createHash("sha256").update(pdfFont).digest("hex"),
    "c3e1564838ecaa70dcfb786e50670fb8cf3ac4e535584f01b0a00fe158931248",
  );
  assert.match(
    read("backend/app/data/fonts/PROVENANCE.md"),
    /101 unique `[.]woff2` files[\s\S]*weight:400[\s\S]*13,635 mapped Unicode code points/,
  );

  for (const skill of [
    "algorithmic-art",
    "canvas-design",
    "frontend-design",
    "mcp-builder",
    "skill-creator",
    "theme-factory",
    "web-artifacts-builder",
    "webapp-testing",
  ]) {
    assert.match(
      read(`backend/app/data/skills/${skill}/LICENSE.txt`),
      /Copyright 2026 Anthropic, PBC\./,
    );
  }
  for (const plugin of [
    "bio-research",
    "cowork-plugin-management",
    "customer-support",
    "data",
    "design",
    "engineering",
    "enterprise-search",
    "finance",
    "human-resources",
    "legal",
    "marketing",
    "operations",
    "product-management",
    "productivity",
    "sales",
  ]) {
    assert.match(
      read(`backend/app/data/plugins/${plugin}/LICENSE`),
      /Apache License\s+Version 2\.0/,
    );
  }

  const baselineReadme = read("release-licenses/README.md");
  assert.match(baselineReadme, /not an automatically generated or exhaustive inventory/i);

  const standalone = "release-licenses/python-runtime/python-build-standalone-20260623";
  assert.deepEqual(
    readdirSync(join(root, standalone, "licenses")).sort(),
    standaloneRuntimeLicenses,
  );
  assert.match(
    read(`${standalone}/build-system/LICENSE.python-build-standalone.MPL-2.0.txt`),
    /Mozilla Public License Version 2\.0/,
  );
  const armMetadata = JSON.parse(
    read(`${standalone}/metadata/PYTHON-aarch64-apple-darwin.json`),
  );
  const intelMetadata = JSON.parse(
    read(`${standalone}/metadata/PYTHON-x86_64-apple-darwin.json`),
  );
  assert.equal(armMetadata.target_triple, "aarch64-apple-darwin");
  assert.equal(armMetadata.apple_sdk_deployment_target, "11.0");
  assert.equal(intelMetadata.target_triple, "x86_64-apple-darwin");
  assert.equal(intelMetadata.apple_sdk_deployment_target, "10.15");
  assert.match(
    read(`${standalone}/checksums.sha256`),
    /3724aa4dafb5f7b6c2cf98e89914e4248dc6bd2fe40407df4a2d73de99615f16/,
  );
});

test("JavaScript runtime packages never fall back to metadata-only notices", () => {
  const report = read("release-licenses/JAVASCRIPT-LICENSES.txt");
  assert.match(
    report,
    /Packages: react-remove-scroll-bar@2\.3\.8[\s\S]{0,500}Copyright \(c\) 2025 Anton Korzunov <thekashey@gmail\.com>/,
  );
  assert.match(
    report,
    /Packages: isarray@1\.0\.0[\s\S]{0,500}Copyright \(c\) 2013 Julian Gruber <julian@juliangruber\.com>/,
  );
  assert.match(
    report,
    /Packages: victory-vendor@37\.3\.6[\s\S]{0,2000}Copyright 2010-2021 Mike Bostock/,
  );

  const metadataOnly = report.slice(
    report.indexOf("Packages whose installed npm artifact has no standalone license file"),
  );
  assert.doesNotMatch(
    metadataOnly,
    /react-remove-scroll-bar@2\.3\.8|isarray@1\.0\.0|victory-vendor@37\.3\.6/,
  );
});

test("source-availability notices match the locked MPL and CDLA versions", () => {
  const availability = read("release-licenses/SOURCE_AVAILABILITY.md");
  const cargoLock = read("desktop-tauri/src-tauri/Cargo.lock");
  const requirements = read("backend/requirements.txt");

  const expectedCargoVersions = {
    cssparser: ["0.29.6", "0.36.0"],
    "cssparser-macros": ["0.6.1"],
    "dtoa-short": ["0.3.5"],
    "option-ext": ["0.2.0"],
    selectors: ["0.24.0", "0.35.0"],
    "webpki-roots": ["1.0.6"],
  };

  for (const [name, expectedVersions] of Object.entries(expectedCargoVersions)) {
    assert.deepEqual(cargoVersions(cargoLock, name), expectedVersions, name);
    for (const version of expectedVersions) {
      assert.match(
        availability,
        new RegExp(`crates\\.io/api/v1/crates/${escapeRegExp(name)}/${escapeRegExp(version)}/download`),
      );
    }
  }

  assert.match(requirements, /^tqdm==4\.68\.4\s+\\$/m);
  assert.match(requirements, /^certifi==2025\.8\.3\s+\\$/m);
  assert.match(availability, /github\.com\/tqdm\/tqdm\/tree\/v4\.68\.4/);
  assert.match(availability, /certifi-2025\.8\.3\.tar\.gz/);
  assert.match(availability, /notofonts\/noto-cjk\/releases\/tag\/Sans2\.004/);
});

test("runtime license filenames stay synchronized with release tooling", () => {
  const nodeDownloader = read("backend/scripts/download_node.py");
  const releaseWorkflow = read(".github/workflows/release.yml");
  const notices = read("THIRD_PARTY_NOTICES.md");

  assert.match(nodeDownloader, /^NODE_VERSION\s*=\s*["']22\.22\.0["']$/m);
  assert.match(releaseWorkflow, /pyinstaller==6\.21\.0/);
  assert.match(releaseWorkflow, /python-version:\s*"3\.12\.13"/);
  assert.match(releaseWorkflow, /uv venv --python 3\.12\.13 --managed-python --seed/);

  for (const filename of requiredThirdPartyFiles) {
    if (filename === "README.md" || filename === "SOURCE_AVAILABILITY.md") continue;
    assert.match(notices, new RegExp(escapeRegExp(filename)));
  }

  assert.match(read("release-licenses/JAVASCRIPT-LICENSES.txt"), /Packages in production graph: 447/);
  assert.match(read("release-licenses/PYTHON-LICENSES.txt"), /Locked packages: 86/);
  assert.match(read("release-licenses/RUST-LICENSES.html"), /suxiaoyou/i);
});

function cargoVersions(lock, packageName) {
  return lock
    .split(/\n(?=\[\[package\]\])/)
    .filter((block) => new RegExp(`^name = "${escapeRegExp(packageName)}"$`, "m").test(block))
    .map((block) => /^version = "([^"]+)"$/m.exec(block)?.[1])
    .filter(Boolean)
    .sort();
}

function escapeRegExp(value) {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}
