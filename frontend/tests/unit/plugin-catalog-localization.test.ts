import assert from "node:assert/strict";
import { existsSync, readFileSync, readdirSync, statSync } from "node:fs";
import { basename, join } from "node:path";
import test from "node:test";

type CatalogEntry = { name?: string; description: string };
type Catalog = {
  connectors: Record<string, CatalogEntry>;
  plugins: Record<string, Required<CatalogEntry>>;
  skills: Record<string, Required<CatalogEntry>>;
};

const zh = JSON.parse(
  readFileSync("src/i18n/locales/zh/plugins.json", "utf8"),
) as Record<string, unknown> & { catalog: Catalog };
const en = JSON.parse(
  readFileSync("src/i18n/locales/en/plugins.json", "utf8"),
) as Record<string, string>;
const content = readFileSync("src/app/(main)/plugins/content.tsx", "utf8");
const connectorHooks = readFileSync("src/hooks/use-connectors.ts", "utf8");
const sidecar = readFileSync(
  "src/lib/plugin-catalog-localization.ts",
  "utf8",
);
const hasChinese = (value: string) => /[\u3400-\u9fff]/u.test(value);

function walk(root: string, filename: string): string[] {
  const matches: string[] = [];
  for (const entry of readdirSync(root)) {
    const path = join(root, entry);
    if (statSync(path).isDirectory()) matches.push(...walk(path, filename));
    else if (entry === filename) matches.push(path);
  }
  return matches;
}

test("Chinese catalog covers every bundled connector and plugin manifest", () => {
  const connectorData = JSON.parse(
    readFileSync("../backend/app/data/connectors.json", "utf8"),
  ) as { connectors?: Record<string, unknown> } & Record<string, unknown>;
  const connectors = connectorData.connectors ?? connectorData;
  assert.deepEqual(
    Object.keys(zh.catalog.connectors).sort(),
    Object.keys(connectors).sort(),
  );
  for (const [id, entry] of Object.entries(zh.catalog.connectors)) {
    assert.ok(hasChinese(entry.description), `${id} needs a Chinese description`);
  }

  const pluginRoot = "../backend/app/data/plugins";
  const pluginIds = readdirSync(pluginRoot)
    .filter((directory) =>
      existsSync(join(pluginRoot, directory, ".claude-plugin", "plugin.json")),
    )
    .sort();
  assert.deepEqual(Object.keys(zh.catalog.plugins).sort(), pluginIds);
  for (const [id, entry] of Object.entries(zh.catalog.plugins)) {
    assert.ok(hasChinese(entry.name), `${id} needs a Chinese display name`);
    assert.ok(hasChinese(entry.description), `${id} needs a Chinese description`);
  }
});

test("Chinese catalog covers bundled and namespaced plugin skills exactly", () => {
  const skillFiles = walk("../backend/app/data/skills", "SKILL.md");
  const pluginSkillFiles = walk("../backend/app/data/plugins", "SKILL.md");
  const expected = new Set<string>();

  for (const path of skillFiles) {
    const source = readFileSync(path, "utf8");
    const name = source.match(/^name:\s*(.+)$/m)?.[1]?.trim();
    assert.ok(name, `missing skill name in ${path}`);
    expected.add(name);
  }
  for (const path of pluginSkillFiles) {
    const source = readFileSync(path, "utf8");
    const name = source.match(/^name:\s*(.+)$/m)?.[1]?.trim();
    assert.ok(name, `missing skill name in ${path}`);
    const relative = path.slice("../backend/app/data/plugins/".length);
    const plugin = relative.split("/")[0] || basename(path);
    expected.add(`${plugin}__${name}`);
  }

  assert.deepEqual(
    Object.keys(zh.catalog.skills).sort(),
    [...expected].sort(),
  );
  for (const [id, entry] of Object.entries(zh.catalog.skills)) {
    assert.ok(hasChinese(entry.name), `${id} needs a Chinese display name`);
    assert.ok(hasChinese(entry.description), `${id} needs a Chinese description`);
  }
});

test("catalog localization is presentation-only and preserves unknown metadata", () => {
  assert.match(sidecar, /defaultValue:\s*fallback/);
  assert.match(sidecar, /replaceAll\(":", "__"\)/);
  assert.match(content, /localizeConnectorDescription/);
  assert.match(content, /localizePluginDescription/);
  assert.match(content, /localizeSkillDescription/);
  assert.match(content, /title=\{skill\.name\}/);
  assert.doesNotMatch(content, />\s*by \{skill\.author\}/);
  assert.doesNotMatch(content, /:\s*"OK"/);
  assert.doesNotMatch(content, /defaultValue/);
  assert.match(
    content,
    /localizeConnectorName\(t, cid, connector\.name\)/,
  );
  assert.match(content, /if \(skill\.catalog_managed\)/);
  assert.match(content, /const useCatalog = skill\.catalog_managed/);
  assert.doesNotMatch(content, /source !== "project"/);
  assert.match(content, /plugin\.source === "builtin"/);
  assert.match(content, /data\.source === "builtin"/);
});

test("plugin UI fallbacks are localized while technical brands remain intact", () => {
  assert.equal(zh.storeByAuthor, "作者：{{author}}");
  assert.equal(zh.storeInstallFailed, "安装失败");
  assert.equal(zh.storeResultCount, "显示 {{shown}} / {{total}}");
  assert.equal(zh.storeViewOnGithub, "在 GitHub 查看");
  assert.equal(zh.storeInstalledBadge, "已安装");
  assert.equal(zh.storeInstall, "安装");
  assert.equal(zh.saveToken, "保存");
  assert.equal(en.storeByAuthor, "by {{author}}");
  assert.equal(en.storeInstallFailed, "Install failed");
  assert.match(zh.catalog.skills["mcp-builder"].name, /MCP/);
  assert.match(zh.catalog.skills["github-deep-research"].name, /GitHub/);
  assert.match(zh.catalog.skills.docx.description, /DOCX/);
});

test("connector mutation fallbacks use the active plugin locale", () => {
  for (const phrase of [
    "Failed to connect connector",
    "Failed to reconnect connector",
    "Failed to approve local connector",
    "Failed to save connector token",
  ]) {
    assert.doesNotMatch(connectorHooks, new RegExp(phrase));
  }
  for (const key of [
    "connectorConnectFailed",
    "connectorReconnectFailed",
    "connectorDisconnectFailed",
    "connectorToggleFailed",
    "localApprovalFailed",
    "connectorTokenSaveFailed",
  ]) {
    assert.match(connectorHooks, new RegExp(`\"${key}\"`));
    assert.equal(typeof en[key], "string");
    assert.equal(typeof zh[key], "string");
    assert.ok(hasChinese(zh[key] as string));
  }
  assert.match(connectorHooks, /error_code/);
  assert.match(connectorHooks, /ensureSuccess/);
  assert.doesNotMatch(connectorHooks, /body\?\.detail/);
});
