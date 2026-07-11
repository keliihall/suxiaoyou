import fs from "node:fs";
import path from "node:path";

import { isMainModule, replaceTomlSectionValues } from "./release-metadata.mjs";

export function syncDesktopMeta(rootDir = process.cwd()) {
  const rootPkgPath = path.join(rootDir, "package.json");
  const tauriConfPath = path.join(rootDir, "desktop-tauri", "src-tauri", "tauri.conf.json");
  const cargoTomlPath = path.join(rootDir, "desktop-tauri", "src-tauri", "Cargo.toml");

  const rootPkg = JSON.parse(fs.readFileSync(rootPkgPath, "utf8"));
  const version = rootPkg.version;
  const description = rootPkg.description;

  const tauriConf = JSON.parse(fs.readFileSync(tauriConfPath, "utf8"));
  tauriConf.version = version;
  tauriConf.productName = "苏小有";
  tauriConf.app.windows = tauriConf.app.windows.map((window) => ({
    ...window,
    title: "苏小有",
  }));
  fs.writeFileSync(tauriConfPath, `${JSON.stringify(tauriConf, null, 2)}\n`);

  const cargoToml = replaceTomlSectionValues(
    fs.readFileSync(cargoTomlPath, "utf8"),
    "package",
    { version, description },
    "desktop-tauri/src-tauri/Cargo.toml",
  );
  fs.writeFileSync(cargoTomlPath, cargoToml);

  console.log(`Synced desktop metadata to version ${version}`);
}

if (isMainModule(import.meta.url)) {
  syncDesktopMeta();
}
