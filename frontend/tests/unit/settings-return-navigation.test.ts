import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const layout = readFileSync("src/app/(main)/layout.tsx", "utf8");
const sidebar = readFileSync(
  "src/components/settings/settings-sidebar.tsx",
  "utf8",
);
const packageJson = JSON.parse(readFileSync("package.json", "utf8")) as {
  scripts: Record<string, string>;
};

test("settings keeps a safe return target instead of calling router.back", () => {
  assert.match(layout, /useState\("\/c\/new"\)/);
  assert.match(layout, /useSearchParams\(\)/);
  assert.match(layout, /pathname\.startsWith\("\/settings"\)/);
  assert.match(layout, /searchParams\.toString\(\)/);
  assert.match(layout, /window\.addEventListener\("hashchange"/);
  assert.match(layout, /<SettingsSidebar returnHref=\{settingsReturnHref\}/);
  assert.doesNotMatch(sidebar, /router\.back\(/);
});

test("the Settings drag strip and full-width return action remain separate", () => {
  assert.match(sidebar, /data-testid="settings-drag-region"/);
  assert.match(sidebar, /data-testid="settings-back-to-app"/);
  assert.match(sidebar, /min-h-11 w-full/);
  assert.match(sidebar, /focus-visible:ring-2/);

  const linkStart = sidebar.indexOf("<Link");
  const linkEnd = sidebar.indexOf("</Link>", linkStart);
  assert.ok(linkStart >= 0 && linkEnd > linkStart);
  assert.doesNotMatch(
    sidebar.slice(linkStart, linkEnd),
    /data-tauri-drag-region/,
  );
});

test("the settings return workflow stays in the release UI gate", () => {
  assert.match(
    packageJson.scripts["test:ui:core"],
    /tests\/ui\/settings-return-navigation\.spec\.ts/,
  );
});
