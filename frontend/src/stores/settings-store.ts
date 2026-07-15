"use client";

import { useState, useEffect } from "react";
import { create } from "zustand";
import { persist } from "zustand/middleware";
import {
  getSavedPermissionDecision,
  migrateSavedPermissionRules,
  savedPermissionRuleKey,
  upsertSavedPermissionRule,
  type SavedPermissionContext,
  type SavedPermissionRule,
  type SavedPermissionRuleInput,
} from "@/lib/saved-permissions";

export type { SavedPermissionRule } from "@/lib/saved-permissions";

export interface PermissionPresets {
  fileChanges: boolean;
  runCommands: boolean;
}

export type ActiveProvider =
  | "byok"
  | "chatgpt"
  | "ollama"
  | "rapid-mlx"
  | "custom"
  | null;

/**
 * Unified work mode — maps to agent + permission presets:
 *   plan → agent=plan (read-only)
 *   ask  → agent=build, auto-approve off
 *   auto → agent=build, auto-approve on
 */
export type WorkMode = "plan" | "ask" | "auto";

interface SettingsStore {
  /** Whether the user has completed the first-run onboarding flow */
  hasCompletedOnboarding: boolean;
  /** Selected model ID (e.g. "claude-sonnet-4-20250514") */
  selectedModel: string | null;
  /** Selected provider ID for the model (e.g. "anthropic") — determines which API key is used */
  selectedProviderId: string | null;
  /** Selected agent name — derived from workMode */
  selectedAgent: string;
  /** Safe mode: read-only analysis, no file changes or commands */
  safeMode: boolean;
  /** Unified work mode */
  workMode: WorkMode;
  /** Whether reasoning/thinking mode is enabled */
  reasoningEnabled: boolean;
  /** Permission presets — auto-allow tool categories */
  permissionPresets: PermissionPresets;
  /** Saved permission rules for specific tools */
  savedPermissions: SavedPermissionRule[];
  /** Workspace directory restriction — agent can only access files inside this dir */
  workspaceDirectory: string | null;
  /** Whether the user has seen the first-use feature hints */
  hasSeenHints: boolean;
  /** UI language code (e.g. "en", "zh") */
  language: string;
  /** Active provider whose models are shown in selectors */
  activeProvider: ActiveProvider;
  /** Set model and its provider */
  setSelectedModel: (model: string | null, providerId?: string | null) => void;
  /** Set agent (for backward compatibility) */
  setSelectedAgent: (agent: string) => void;
  /** Toggle safe mode on/off */
  setSafeMode: (enabled: boolean) => void;
  /** Set unified work mode (plan / ask / auto) */
  setWorkMode: (mode: WorkMode) => void;
  /** Set reasoning mode */
  setReasoningEnabled: (enabled: boolean) => void;
  /** Toggle a single permission preset */
  togglePermissionPreset: (key: keyof PermissionPresets) => void;
  /** Save one exact permission decision in its original invocation scope. */
  savePermissionRule: (rule: SavedPermissionRuleInput) => void;
  /** Get an exact saved decision in one invocation scope (if any). */
  getSavedPermission: (
    tool: string,
    pattern: string,
    context: SavedPermissionContext,
  ) => boolean | null;
  /** Clear a saved permission rule */
  clearPermissionRule: (rule: SavedPermissionRule) => void;
  /** Clear all saved permission rules */
  clearAllPermissionRules: () => void;
  /** Set workspace directory (null = unrestricted) */
  setWorkspaceDirectory: (dir: string | null) => void;
  /** Mark onboarding as complete */
  completeOnboarding: () => void;
  /** Mark feature hints as seen */
  setHasSeenHints: (seen: boolean) => void;
  /** Set UI language */
  setLanguage: (lang: string) => void;
  /** Set active provider */
  setActiveProvider: (provider: ActiveProvider) => void;
}

export const useSettingsStore = create<SettingsStore>()(
  persist(
    (set, get) => ({
      hasCompletedOnboarding: false,
      selectedModel: null,
      selectedProviderId: null,
      selectedAgent: "build",
      safeMode: false,
      workMode: "ask" as WorkMode,
      reasoningEnabled: true,
      permissionPresets: { fileChanges: false, runCommands: false },
      savedPermissions: [],
      workspaceDirectory: null,
      hasSeenHints: false,
      language: "auto",
      activeProvider: null,
      setSelectedModel: (model, providerId) =>
        set({ selectedModel: model, selectedProviderId: providerId ?? null }),
      setSelectedAgent: (agent) => {
        const isPlan = agent === "plan";
        const currentPresets = get().permissionPresets;
        const mode: WorkMode = isPlan
          ? "plan"
          : currentPresets.fileChanges
            ? "auto"
            : "ask";
        set({ selectedAgent: agent, safeMode: isPlan, workMode: mode });
      },
      setSafeMode: (enabled) => {
        const currentPresets = get().permissionPresets;
        const mode: WorkMode = enabled
          ? "plan"
          : currentPresets.fileChanges
            ? "auto"
            : "ask";
        set({
          safeMode: enabled,
          selectedAgent: enabled ? "plan" : "build",
          workMode: mode,
        });
      },
      setWorkMode: (mode) => {
        switch (mode) {
          case "plan":
            set({
              workMode: "plan",
              selectedAgent: "plan",
              safeMode: true,
              permissionPresets: { fileChanges: false, runCommands: false },
            });
            break;
          case "ask":
            set({
              workMode: "ask",
              selectedAgent: "build",
              safeMode: false,
              permissionPresets: { fileChanges: false, runCommands: false },
            });
            break;
          case "auto":
            set({
              workMode: "auto",
              selectedAgent: "build",
              safeMode: false,
              permissionPresets: { fileChanges: true, runCommands: true },
            });
            break;
        }
      },
      setReasoningEnabled: (enabled) => set({ reasoningEnabled: enabled }),
      togglePermissionPreset: (key) =>
        set((s) => {
          const next = {
            ...s.permissionPresets,
            [key]: !s.permissionPresets[key],
          };
          // Sync workMode (only if currently in build agent)
          const workMode: WorkMode =
            s.selectedAgent === "plan"
              ? "plan"
              : next.fileChanges
                ? "auto"
                : "ask";
          return { permissionPresets: next, workMode };
        }),
      savePermissionRule: (rule) =>
        set((s) => ({
          savedPermissions: upsertSavedPermissionRule(
            s.savedPermissions,
            rule,
          ),
        })),
      getSavedPermission: (tool, pattern, context) =>
        getSavedPermissionDecision(
          get().savedPermissions,
          tool,
          pattern,
          context,
        ),
      clearPermissionRule: (rule) =>
        set((s) => ({
          savedPermissions: s.savedPermissions.filter(
            (candidate) =>
              savedPermissionRuleKey(candidate) !== savedPermissionRuleKey(rule),
          ),
        })),
      clearAllPermissionRules: () => set({ savedPermissions: [] }),
      setWorkspaceDirectory: (dir) => set({ workspaceDirectory: dir }),
      completeOnboarding: () => {
        // A workspace is part of the first-run security boundary. Keep the
        // onboarding gate closed if a stale caller tries to bypass the UI.
        const workspace = get().workspaceDirectory;
        const normalizedWorkspace = workspace?.trim();
        if (!normalizedWorkspace || normalizedWorkspace === ".") return;
        set({ hasCompletedOnboarding: true });
      },
      setHasSeenHints: (seen) => set({ hasSeenHints: seen }),
      setLanguage: (lang) => {
        set({ language: lang });
        localStorage.setItem("suxiaoyou-language", lang);
        // Dynamic import to avoid circular dependency
        import("@/i18n/config").then((mod) => mod.default.changeLanguage(lang));
      },
      setActiveProvider: (provider) => set({ activeProvider: provider }),
    }),
    {
      name: "suxiaoyou-settings",
      version: 5,
      migrate: (persistedState, persistedVersion) => {
        if (!persistedState || typeof persistedState !== "object") {
          return persistedState as SettingsStore;
        }

        const state = { ...persistedState } as Partial<SettingsStore>;
        const legacyActiveProvider = (
          persistedState as Record<string, unknown>
        ).activeProvider;
        if (legacyActiveProvider === "suxiaoyou") {
          state.activeProvider = null;
        }
        if (legacyActiveProvider === "local") {
          state.activeProvider = "custom";
        }

        if (persistedVersion < 4) {
          const normalizedWorkspace =
            typeof state.workspaceDirectory === "string"
              ? state.workspaceDirectory.trim()
              : "";
          const hasWorkspace =
            normalizedWorkspace.length > 0 && normalizedWorkspace !== ".";

          Object.assign(state, {
            ...state,
            // v0.9 resets the inherited Auto default to the fail-safe Ask
            // posture. Plan remains read-only; users can explicitly opt back
            // into Auto after the migration.
            ...(state.workMode === "plan"
              ? {
                  selectedAgent: "plan",
                  safeMode: true,
                  permissionPresets: {
                    fileChanges: false,
                    runCommands: false,
                  },
                }
              : {
                  selectedAgent: "build",
                  safeMode: false,
                  workMode: "ask" as WorkMode,
                  permissionPresets: {
                    fileChanges: false,
                    runCommands: false,
                  },
                }),
            // Existing installations that never chose a workspace must pass
            // through the new workspace-first onboarding once.
            hasCompletedOnboarding:
              Boolean(state.hasCompletedOnboarding) && hasWorkspace,
          });
        }

        if (persistedVersion < 5) {
          // v4 remembered only a tool name and later replayed every decision
          // as pattern="*". Discard those ambiguous entries; retain only
          // already-explicit patterns that can be scoped to the workspace.
          state.savedPermissions = migrateSavedPermissionRules(
            state.savedPermissions,
            state.workspaceDirectory,
          );
        }

        return state as SettingsStore;
      },
    },
  ),
);

// Hydration tracking
const useSettingsHasHydrated = () => {
  const [hydrated, setHydrated] = useState(false);
  useEffect(() => {
    if (!useSettingsStore.persist) {
      setHydrated(true);
      return;
    }
    if (useSettingsStore.persist.hasHydrated()) {
      setHydrated(true);
    }
    const unsub = useSettingsStore.persist.onFinishHydration(() =>
      setHydrated(true),
    );
    return () => {
      unsub();
    };
  }, []);
  return hydrated;
};

export { useSettingsHasHydrated };
