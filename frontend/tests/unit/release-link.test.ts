import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import {
  LATEST_RELEASE_URL,
  openLatestReleasePage,
} from "../../src/lib/release-link.ts";

test("desktop releases use the native open_external bridge", async () => {
  const desktopCalls: string[] = [];
  const webCalls: unknown[][] = [];

  await openLatestReleasePage({
    desktop: true,
    openDesktop: async (url) => { desktopCalls.push(url); },
    openWeb: (...args) => { webCalls.push(args); },
  });

  assert.deepEqual(desktopCalls, [LATEST_RELEASE_URL]);
  assert.deepEqual(webCalls, []);
});

test("web releases open a noopener window without initiating a download", async () => {
  const webCalls: unknown[][] = [];

  await openLatestReleasePage({
    desktop: false,
    openDesktop: async () => { throw new Error("desktop bridge should not run"); },
    openWeb: (...args) => { webCalls.push(args); },
  });

  assert.deepEqual(webCalls, [
    [LATEST_RELEASE_URL, "_blank", "noopener,noreferrer"],
  ]);
});

test("About names the download destination and uses the existing desktop bridge", () => {
  const general = readFileSync("src/components/settings/general-tab.tsx", "utf8");
  const en = JSON.parse(readFileSync("src/i18n/locales/en/settings.json", "utf8"));
  const zh = JSON.parse(readFileSync("src/i18n/locales/zh/settings.json", "utf8"));

  assert.match(general, /desktopAPI\.openExternal\(url\)/);
  assert.equal(en.aboutReleasesAction, "View latest version and downloads");
  assert.equal(zh.aboutReleasesAction, "查看最新版本与下载");
  assert.match(zh.aboutReleasesDesc, /不会自动下载或安装/);
});
