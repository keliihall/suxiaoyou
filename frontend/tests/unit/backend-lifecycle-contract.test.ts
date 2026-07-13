import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import test from "node:test";

const hookSource = readFileSync("src/hooks/use-backend-lifecycle.ts", "utf8");
const providerSource = readFileSync(
  "src/components/providers/app-providers.tsx",
  "utf8",
);
const bridgeSource = readFileSync("src/lib/tauri-api.ts", "utf8");
const screenSource = readFileSync(
  "src/components/desktop/backend-status-screen.tsx",
  "utf8",
);
const titleBarSource = readFileSync(
  "src/components/desktop/title-bar.tsx",
  "utf8",
);
const mainLayoutSource = readFileSync("src/app/(main)/layout.tsx", "utf8");
const globalErrorSource = readFileSync("src/app/global-error.tsx", "utf8");
const constantsSource = readFileSync("src/lib/constants.ts", "utf8");
const platformHookSource = readFileSync("src/hooks/use-platform.ts", "utf8");
const traySource = readFileSync("../desktop-tauri/src-tauri/src/tray.rs", "utf8");
const nativeLibSource = readFileSync(
  "../desktop-tauri/src-tauri/src/lib.rs",
  "utf8",
);
const nativeCommandsSource = readFileSync(
  "../desktop-tauri/src-tauri/src/commands.rs",
  "utf8",
);
const nativeBackendSource = readFileSync(
  "../desktop-tauri/src-tauri/src/backend.rs",
  "utf8",
);
const backendLauncherSource = readFileSync("../backend/run.py", "utf8");
const backendSpecSource = readFileSync("../backend/suxiaoyou.spec", "utf8");
const nativeCapabilities = readFileSync(
  "../desktop-tauri/src-tauri/capabilities/default.json",
  "utf8",
);
const nativeMenuSource = readFileSync(
  "../desktop-tauri/src-tauri/src/menu.rs",
  "utf8",
);
const automationSources = [
  "src/app/(main)/automations/automation-card.tsx",
  "src/app/(main)/automations/shared-ui.tsx",
].map((path) => readFileSync(path, "utf8")).join("\n");

test("desktop lifecycle is driven only by the native snapshot and event", () => {
  assert.match(hookSource, /onBackendStatus\(applyStatus\)/);
  assert.match(hookSource, /getBackendStatus\(\)/);
  assert.match(hookSource, /type: "native-status"/);
  assert.doesNotMatch(hookSource, /@\/lib\/api/);
  assert.doesNotMatch(hookSource, /@tanstack\/react-query/);
  assert.doesNotMatch(hookSource, /provider/i);
});

test("stale lifecycle snapshots are rejected before backend cache side effects", () => {
  const staleGuard = hookSource.indexOf(
    "status.revision <= latestRevisionRef.current",
  );
  const urlCacheUpdate = hookSource.indexOf("resetBackendUrl(status.url)");
  const tokenCacheReset = hookSource.indexOf("resetBackendToken()");

  assert.ok(staleGuard >= 0, "hook must guard stale native revisions");
  assert.ok(staleGuard < urlCacheUpdate, "stale guard must precede URL cache writes");
  assert.ok(staleGuard < tokenCacheReset, "stale guard must precede token cache writes");
});

test("app providers keep content mounted after Ready and show lifecycle UI", () => {
  assert.match(providerSource, /hasEverBeenReady/);
  assert.match(providerSource, /BackendStatusScreen/);
  assert.match(providerSource, /backend\.status\.phase === "ready"/);
  assert.doesNotMatch(providerSource, /getBackendUrl\(\)[\s\S]*\.finally/);
  assert.match(providerSource, /Language loading must never leave the entire application blank/);
});

test("desktop recovery chrome stays outside the lifecycle gate and above its overlay", () => {
  assert.match(providerSource, /showDesktopLifecycle && <TitleBar recoveryActive=/);
  assert.match(providerSource, /inert=\{recoveryActive \? true : undefined\}/);
  assert.doesNotMatch(mainLayoutSource, /<TitleBar/);
  assert.match(globalErrorSource, /<TitleBar recoveryActive appName=\{copy\.appName\} \/>/);
  assert.match(titleBarSource, /data-tauri-drag-region/);
  assert.match(titleBarSource, /z-\[10000\]/);
  assert.match(titleBarSource, /isChatPage && !recoveryActive/);
  assert.match(titleBarSource, /platform === "unknown"/);
  assert.match(platformHookSource, /detectBrowserPlatform/);
  assert.match(platformHookSource, /Native lookup failed/);
});

test("web language bootstrap never renders desktop local-service status", () => {
  assert.match(providerSource, /runtimeReady && IS_DESKTOP/);
  assert.match(
    providerSource,
    /\{showDesktopLifecycle && \(\s*<BackendStatusScreen/,
  );

  const languageTry = providerSource.indexOf("try {");
  const preferenceRead = providerSource.indexOf("getClientLanguagePreference()");
  assert.ok(languageTry >= 0 && languageTry < preferenceRead);
});

test("backend URL and token resets fence stale in-flight cache writes", () => {
  assert.match(constantsSource, /let _backendUrlGeneration = 0/);
  assert.match(constantsSource, /let _backendTokenGeneration = 0/);
  assert.match(constantsSource, /generation !== _backendUrlGeneration.*getBackendUrl/);
  assert.match(constantsSource, /generation !== _backendTokenGeneration.*getBackendToken/);
  assert.match(
    constantsSource,
    /resetBackendUrl[\s\S]*?_backendUrlGeneration \+= 1/,
  );
  assert.match(
    constantsSource,
    /resetBackendToken[\s\S]*?_backendTokenGeneration \+= 1/,
  );
});

test("desktop tray and automation links use static-export-compatible chat routes", () => {
  assert.match(traySource, /\/c\/_\?sessionId=/);
  assert.doesNotMatch(traySource, /format!\("\/c\/\{session_id\}"\)/);
  assert.match(automationSources, /getChatRoute/);
  assert.doesNotMatch(automationSources, /href=\{`\/c\/\$\{/);
});

test("desktop bridge exposes snapshot, event, fixed logs, and whole-app relaunch", () => {
  assert.match(bridgeSource, /invoke<BackendStatus>\("get_backend_status"\)/);
  assert.match(bridgeSource, /listen<BackendStatus>\("backend-status"/);
  assert.match(bridgeSource, /invoke<void>\("open_backend_logs"\)/);
  assert.match(bridgeSource, /invoke<void>\("relaunch_app"\)/);
  assert.match(nativeCommandsSource, /pub async fn relaunch_app/);
  assert.match(nativeCommandsSource, /state\.stop\(\)\.await\?/);
  assert.match(nativeCommandsSource, /app\.restart\(\)/);
  assert.match(nativeLibSource, /api\.prevent_exit\(\)/);
  assert.match(nativeLibSource, /exit_cleanup_complete/);
  assert.match(nativeMenuSource, /"menu_quit"/);
  assert.match(nativeMenuSource, /app\.exit\(0\)/);
  assert.doesNotMatch(nativeMenuSource, /PredefinedMenuItem::quit/);
  assert.doesNotMatch(nativeCapabilities, /process:allow-(?:exit|restart)/);
  assert.doesNotMatch(bridgeSource, /@tauri-apps\/plugin-process/);
});

test("packaged backend is isolated and bound to the desktop parent lifetime", () => {
  assert.match(nativeBackendSource, /\.process_group\(0\)/);
  assert.match(
    nativeBackendSource,
    /SUXIAOYOU_DESKTOP_PARENT_PID[\s\S]*std::process::id\(\)/,
  );
  assert.match(backendLauncherSource, /process_group != process_id/);
  assert.match(backendLauncherSource, /_PROCESS_GROUP_GRACE_SECONDS = 12\.0/);
  assert.match(
    backendLauncherSource,
    /job_object_limit_kill_on_job_close = 0x00002000/,
  );
  assert.match(backendLauncherSource, /WaitForSingleObject/);
  assert.match(backendLauncherSource, /wait_result == wait_object_0/);
  assert.match(backendSpecSource, /console=sys\.platform == ['"]win32['"]/);
  assert.match(nativeBackendSource, /const CREATE_NO_WINDOW: u32 = 0x08000000/);
  assert.match(
    nativeBackendSource,
    /creation_flags\(CREATE_NEW_PROCESS_GROUP \| CREATE_NO_WINDOW\)/,
  );
});

test("failure screen and root global error provide visible recovery controls", () => {
  assert.match(screenSource, /backendFailedTitle/);
  assert.match(screenSource, /onRelaunch/);
  assert.match(screenSource, /onOpenLogs/);
  assert.match(screenSource, /backendTechnicalDetails/);
  assert.equal(existsSync("src/app/global-error.tsx"), true);

  for (const locale of ["en", "zh"]) {
    const common = JSON.parse(
      readFileSync(`src/i18n/locales/${locale}/common.json`, "utf8"),
    );
    for (const key of [
      "backendInitializingTitle",
      "backendRestartingTitle",
      "backendFailedTitle",
      "backendRetry",
      "backendOpenLogs",
      "backendTechnicalDetails",
    ]) {
      assert.equal(typeof common[key], "string", `${locale}.${key}`);
      assert.ok(common[key].length > 0, `${locale}.${key}`);
    }
  }
});
