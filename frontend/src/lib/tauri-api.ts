/**
 * Tauri API bridge — replaces the Electron preload API.
 *
 * Uses Tauri's `invoke` and `listen` under the hood.
 */

import { invoke } from "@tauri-apps/api/core";
import { listen, type UnlistenFn } from "@tauri-apps/api/event";
import type { BackendStatus } from "./backend-lifecycle";

export interface TrayRecent {
  id: string;
  title: string | null;
}

export interface DesktopAPI {
  getBackendUrl: () => Promise<string>;
  getBackendToken: () => Promise<string>;
  getBackendStatus: () => Promise<BackendStatus>;
  getPendingNavigation: () => Promise<string | null>;
  getPlatform: () => Promise<string>;
  openBackendLogs: () => Promise<void>;
  relaunch: () => Promise<void>;
  openExternal: (url: string) => Promise<void>;
  downloadAndSave: (opts: { url?: string; data?: number[]; defaultName: string }) => Promise<boolean>;
  saveAuthorizedFileAs: (opts: {
    path: string;
    sessionId: string;
    defaultName: string;
    dialogTitle: string;
  }) => Promise<boolean>;
  openAuthorizedFileWith: (opts: {
    path: string;
    sessionId: string;
    dialogTitle: string;
  }) => Promise<boolean>;
  minimize: () => Promise<void>;
  maximize: () => Promise<void>;
  close: () => Promise<void>;
  isMaximized: () => Promise<boolean>;
  updateTrayRecents: (recents: TrayRecent[]) => Promise<void>;
  onMaximizeChange: (callback: (maximized: boolean) => void) => () => void;
  onBackendRestarting: (callback: () => void) => () => void;
  onBackendRestart: (callback: (newUrl: string) => void) => () => void;
  onBackendCrashLog: (callback: (log: string) => void) => () => void;
  onBackendStatus: (
    callback: (status: BackendStatus) => void,
  ) => Promise<() => void>;
  onNavigate: (callback: (path: string) => void) => () => void;
  onToggleSidebar: (callback: () => void) => () => void;
  onOpenSearch: (callback: () => void) => () => void;
}

/** Helper to turn a Tauri `listen` promise into a sync cleanup function. */
function listenSync<T>(
  event: string,
  handler: (payload: T) => void
): () => void {
  let unlisten: UnlistenFn | null = null;
  let cancelled = false;

  listen<T>(event, (e) => handler(e.payload)).then((fn) => {
    if (cancelled) {
      fn();
    } else {
      unlisten = fn;
    }
  });

  return () => {
    cancelled = true;
    unlisten?.();
  };
}

export const desktopAPI: DesktopAPI = {
  getBackendUrl: () => invoke<string>("get_backend_url"),
  getBackendToken: () => invoke<string>("get_backend_token"),
  getBackendStatus: () => invoke<BackendStatus>("get_backend_status"),
  getPendingNavigation: () => invoke<string | null>("get_pending_navigation"),
  getPlatform: () => invoke<string>("get_platform"),
  openBackendLogs: () => invoke<void>("open_backend_logs"),
  relaunch: () => invoke<void>("relaunch_app"),
  openExternal: (url) => invoke("open_external", { url }),
  downloadAndSave: ({ url, data, defaultName }) => invoke<boolean>("download_and_save", { url, data, defaultName }),
  saveAuthorizedFileAs: ({ path, sessionId, defaultName, dialogTitle }) =>
    invoke<boolean>("save_authorized_file_as", {
      path,
      sessionId,
      defaultName,
      dialogTitle,
    }),
  openAuthorizedFileWith: ({ path, sessionId, dialogTitle }) =>
    invoke<boolean>("open_authorized_file_with", { path, sessionId, dialogTitle }),
  minimize: () => invoke("window_minimize"),
  maximize: () => invoke("window_maximize"),
  close: () => invoke("window_close"),
  isMaximized: () => invoke<boolean>("is_maximized"),
  updateTrayRecents: (recents) => invoke("update_tray_recents", { recents }),
  onMaximizeChange: (callback) =>
    listenSync<boolean>("maximize-change", callback),
  onBackendRestarting: (callback) =>
    listenSync<void>("backend-restarting", callback),
  onBackendRestart: (callback) =>
    listenSync<string>("backend-restart", callback),
  onBackendCrashLog: (callback) =>
    listenSync<string>("backend-crash-log", callback),
  onBackendStatus: async (callback) =>
    listen<BackendStatus>("backend-status", (event) => callback(event.payload)),
  onNavigate: (callback) => listenSync<string>("navigate", callback),
  onToggleSidebar: (callback) => listenSync<void>("toggle-sidebar", callback),
  onOpenSearch: (callback) => listenSync<void>("open-search", callback),
};
