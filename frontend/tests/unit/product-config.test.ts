import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import test from "node:test";
import { inflateSync } from "node:zlib";

function readJson<T>(path: string): T {
  return JSON.parse(readFileSync(path, "utf8")) as T;
}

function readText(path: string): string {
  return readFileSync(path, "utf8");
}

function readRgbaPng(path: string): { width: number; height: number; pixels: number[][][] } {
  const data = readFileSync(path);
  assert.equal(data.subarray(0, 8).toString("hex"), "89504e470d0a1a0a");

  let offset = 8;
  let width = 0;
  let height = 0;
  let colorType = -1;
  const idat: Buffer[] = [];
  while (offset < data.length) {
    const length = data.readUInt32BE(offset);
    const type = data.subarray(offset + 4, offset + 8).toString("ascii");
    const chunk = data.subarray(offset + 8, offset + 8 + length);
    offset += 12 + length;
    if (type === "IHDR") {
      width = chunk.readUInt32BE(0);
      height = chunk.readUInt32BE(4);
      assert.equal(chunk[8], 8);
      colorType = chunk[9];
      assert.equal(colorType, 6);
    } else if (type === "IDAT") {
      idat.push(chunk);
    } else if (type === "IEND") {
      break;
    }
  }

  const raw = inflateSync(Buffer.concat(idat));
  const bpp = 4;
  const stride = width * bpp;
  const pixels: number[][][] = [];
  let pos = 0;
  let previous = new Uint8Array(stride);

  for (let y = 0; y < height; y += 1) {
    const filter = raw[pos];
    pos += 1;
    const row = new Uint8Array(raw.subarray(pos, pos + stride));
    pos += stride;
    const out = new Uint8Array(stride);
    for (let x = 0; x < stride; x += 1) {
      const left = x >= bpp ? out[x - bpp] : 0;
      const up = previous[x];
      const upLeft = x >= bpp ? previous[x - bpp] : 0;
      let predictor = 0;
      if (filter === 1) predictor = left;
      else if (filter === 2) predictor = up;
      else if (filter === 3) predictor = Math.floor((left + up) / 2);
      else if (filter === 4) {
        const p = left + up - upLeft;
        const pa = Math.abs(p - left);
        const pb = Math.abs(p - up);
        const pc = Math.abs(p - upLeft);
        predictor = pa <= pb && pa <= pc ? left : pb <= pc ? up : upLeft;
      } else {
        assert.equal(filter, 0);
      }
      out[x] = (row[x] + predictor) & 255;
    }
    pixels.push([]);
    for (let x = 0; x < stride; x += bpp) {
      pixels[y].push([out[x], out[x + 1], out[x + 2], out[x + 3]]);
    }
    previous = out;
  }

  return { width, height, pixels };
}

test("all release metadata matches the root product version", () => {
  const rootPackage = readJson<{ version: string }>("../package.json");
  const rootPackageLock = readJson<{ version: string; packages: { "": { version: string } } }>(
    "../package-lock.json",
  );
  const frontendPackage = readJson<{ version: string }>("package.json");
  const frontendPackageLock = readJson<{
    version: string;
    packages: { "": { version: string } };
  }>(
    "package-lock.json",
  );
  const pyproject = readText("../backend/pyproject.toml");
  const tauriConfig = readJson<{ version: string }>("../desktop-tauri/src-tauri/tauri.conf.json");
  const cargoToml = readText("../desktop-tauri/src-tauri/Cargo.toml");
  const cargoLock = readText("../desktop-tauri/src-tauri/Cargo.lock");
  const zhCommon = readJson<{ poweredBy: string }>("src/i18n/locales/zh/common.json");
  const enCommon = readJson<{ poweredBy: string }>("src/i18n/locales/en/common.json");

  const backendVersion = pyproject.match(/\[project\][\s\S]*?^version = "([^"]+)"/m)?.[1];
  const cargoVersion = cargoToml.match(/\[package\][\s\S]*?^version = "([^"]+)"/m)?.[1];
  const cargoLockVersion = cargoLock.match(
    /\[\[package\]\]\s+name = "suxiaoyou-desktop"\s+version = "([^"]+)"/,
  )?.[1];

  for (const [source, version] of [
    ["package-lock.json top-level version", rootPackageLock.version],
    ["package-lock.json root entry", rootPackageLock.packages[""].version],
    ["frontend/package.json", frontendPackage.version],
    ["frontend/package-lock.json top-level version", frontendPackageLock.version],
    ["frontend/package-lock.json root entry", frontendPackageLock.packages[""].version],
    ["backend/pyproject.toml", backendVersion],
    ["desktop-tauri/src-tauri/tauri.conf.json", tauriConfig.version],
    ["desktop-tauri/src-tauri/Cargo.toml", cargoVersion],
    ["desktop-tauri/src-tauri/Cargo.lock", cargoLockVersion],
  ]) {
    assert.equal(version, rootPackage.version, source);
  }

  assert.equal(zhCommon.poweredBy, `苏小有 v${rootPackage.version}`);
  assert.equal(enCommon.poweredBy, `苏小有 v${rootPackage.version}`);
});

test("settings version fallback is derived from frontend/package.json", () => {
  const generalTab = readText("src/components/settings/general-tab.tsx");

  assert.match(generalTab, /from ["']\.\.\/\.\.\/\.\.\/package\.json["']/);
  assert.match(generalTab, /useState\([^)]*\.version\)/);
  assert.doesNotMatch(generalTab, /useState\(["']\d+\.\d+\.\d+["']\)/);
});

test("about copy matches the 苏小有 product", () => {
  const zhSettings = readJson<Record<string, string>>("src/i18n/locales/zh/settings.json");
  const enSettings = readJson<Record<string, string>>("src/i18n/locales/en/settings.json");

  assert.equal(zhSettings.aboutVersion, "苏小有 v{{version}}");
  assert.equal(zhSettings.aboutDesc, "“小有所成，小有智慧。”  面向桌面端工作的AI助理。");
  assert.equal(zhSettings.aboutCopyright, "苏小有 © 2026");
  assert.equal(enSettings.aboutVersion, "苏小有 v{{version}}");
  assert.equal(enSettings.aboutDesc, "“小有所成，小有智慧。”  面向桌面端工作的AI助理。");
  assert.equal(enSettings.aboutCopyright, "苏小有 © 2026");
});

test("frontend fonts remain reproducible without network access", () => {
  const rootLayout = readText("src/app/layout.tsx");
  const globals = readText("src/app/globals.css");

  assert.doesNotMatch(rootLayout, /next\/font\/google/);
  assert.doesNotMatch(rootLayout, /Inter|JetBrains_Mono|Noto_Sans_SC/);
  assert.match(globals, /--font-sans:[^;]*-apple-system[^;]*"Segoe UI"[^;]*"Microsoft YaHei"[^;]*sans-serif;/);
  assert.match(globals, /--font-mono:[^;]*ui-monospace[^;]*"Cascadia Code"[^;]*monospace;/);
});

test("check for updates entry points are removed from visible UI", () => {
  const generalTab = readText("src/components/settings/general-tab.tsx");
  const menu = readText("../desktop-tauri/src-tauri/src/menu.rs");
  const tray = readText("../desktop-tauri/src-tauri/src/tray.rs");
  const mainLayout = readText("src/app/(main)/layout.tsx");
  const sidebarFooter = readText("src/components/layout/sidebar-footer.tsx");
  const tauriApi = readText("src/lib/tauri-api.ts");
  const tauriLib = readText("../desktop-tauri/src-tauri/src/lib.rs");
  const tauriConfig = readText("../desktop-tauri/src-tauri/tauri.conf.json");
  const cargoToml = readText("../desktop-tauri/src-tauri/Cargo.toml");
  const cargoLock = readText("../desktop-tauri/src-tauri/Cargo.lock");
  const frontendPackage = readText("package.json");
  const frontendPackageLock = readText("package-lock.json");
  const zhSettings = readText("src/i18n/locales/zh/settings.json");
  const enSettings = readText("src/i18n/locales/en/settings.json");

  assert.equal(existsSync("src/hooks/use-update-check.ts"), false);
  assert.equal(existsSync("src/components/desktop/update-banner.tsx"), false);

  for (const source of [
    generalTab,
    menu,
    tray,
    mainLayout,
    sidebarFooter,
    tauriApi,
    tauriLib,
    tauriConfig,
    cargoToml,
    cargoLock,
    frontendPackage,
    frontendPackageLock,
    zhSettings,
    enSettings,
  ]) {
    assert.doesNotMatch(source, /check-for-updates/i);
    assert.doesNotMatch(source, /check_updates/i);
    assert.doesNotMatch(source, /checkForUpdates/);
    assert.doesNotMatch(source, /Check for Updates/i);
    assert.doesNotMatch(source, /UpdateBanner/);
    assert.doesNotMatch(source, /useUpdateCheck/);
    assert.doesNotMatch(source, /plugin-updater/);
    assert.doesNotMatch(source, /tauri-plugin-updater/);
    assert.doesNotMatch(source, /createUpdaterArtifacts/);
    assert.doesNotMatch(source, /updateAvailable/);
    assert.doesNotMatch(source, /updateNow/);
  }
});

test("dark theme defaults use black background, white foreground, and blue accent", () => {
  const appearanceStore = readText("src/stores/appearance-store.ts");
  const globals = readText("src/app/globals.css");

  assert.match(
    appearanceStore,
    /dark:\s*\{\s*accent:\s*"#339CFF",\s*background:\s*"#000000",\s*foreground:\s*"#FFFFFF"\s*\}/,
  );
  assert.match(globals, /\.dark\s*\{[\s\S]*--surface-primary:\s*#000000;/);
  assert.match(globals, /\.dark\s*\{[\s\S]*--surface-chat:\s*#000000;/);
  assert.match(globals, /\.dark\s*\{[\s\S]*--text-primary:\s*#FFFFFF;/);
  assert.match(globals, /\.dark\s*\{[\s\S]*--brand-primary:\s*#339CFF;/);
});

test("dark preset sidebar remains readable under macOS vibrancy", () => {
  const globals = readText("src/app/globals.css");
  const darkBlock = globals.match(/\.dark\s*\{([\s\S]*?)\n\}/)?.[1] ?? "";

  assert.match(darkBlock, /--sidebar-translucent-bg:\s*rgba\(0,\s*0,\s*0,\s*0\.88\);/);
  assert.doesNotMatch(darkBlock, /--sidebar-translucent-bg:\s*rgba\(0,\s*0,\s*0,\s*0\.20\);/);
});

test("macOS tray template icon uses a detailed black alpha mask", () => {
  const oneX = readRgbaPng("../desktop-tauri/src-tauri/icons/tray-template.png");
  const twoX = readRgbaPng("../desktop-tauri/src-tauri/icons/tray-template@2x.png");

  assert.deepEqual([oneX.width, oneX.height], [22, 22]);
  assert.deepEqual([twoX.width, twoX.height], [44, 44]);

  for (const image of [oneX, twoX]) {
    const opaque = image.pixels.flat().filter(([, , , alpha]) => alpha > 0);
    assert.ok(opaque.length > 0);
    assert.ok(opaque.every(([red, green, blue]) => red === 0 && green === 0 && blue === 0));
  }

  assert.equal(twoX.pixels[18][22][3], 0);
  assert.ok(twoX.pixels[25][22][3] > 0);
  assert.equal(twoX.pixels[38][22][3], 0);
  assert.ok(twoX.pixels[38][10][3] > 0);
  assert.ok(twoX.pixels[38][34][3] > 0);
});

test("native macOS tray and application menus use Chinese visible labels", () => {
  const tray = readText("../desktop-tauri/src-tauri/src/tray.rs");
  const menu = readText("../desktop-tauri/src-tauri/src/menu.rs");
  const nativeSources = `${tray}\n${menu}`;

  for (const label of [
    "New Chat",
    "Search Chats",
    "Recent Chats",
    "No recent chats",
    "Show All Chats",
    "Untitled chat",
    "Open 苏小有",
    "Settings",
    "Reload",
    "Quit 苏小有",
    "Toggle Sidebar",
    "Developer Tools",
    "About 苏小有",
  ]) {
    assert.doesNotMatch(nativeSources, new RegExp(label.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")));
  }

  for (const label of [
    "新对话",
    "搜索对话…",
    "最近对话",
    "暂无最近对话",
    "显示全部对话",
    "未命名对话",
    "打开苏小有",
    "设置",
    "重新加载",
    "退出苏小有",
    "切换侧边栏",
    "开发者工具",
    "关于苏小有",
  ]) {
    assert.match(nativeSources, new RegExp(label.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")));
  }
});

test("native macOS install warning is localized", () => {
  const lib = readText("../desktop-tauri/src-tauri/src/lib.rs");

  assert.doesNotMatch(lib, /running from the DMG volume/);
  assert.doesNotMatch(lib, /copy .*Applications/);
  assert.doesNotMatch(lib, /Install 苏小有 to Applications/);
  assert.match(lib, /苏小有正在从 DMG 磁盘映像中运行/);
  assert.match(lib, /请先将苏小有\.app 复制到“应用程序”文件夹/);
  assert.match(lib, /将苏小有安装到应用程序/);
});
