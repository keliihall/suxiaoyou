"use client";

import { useCallback, useEffect, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { BadgeCheck, ChevronDown, GitBranch, History, Loader2, RotateCcw, ShieldAlert } from "lucide-react";
import { useTranslation } from "react-i18next";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { ApiError, api } from "@/lib/api";
import { API } from "@/lib/constants";
import { cn } from "@/lib/utils";
import { useWorkspaceStore } from "@/stores/workspace-store";

interface RuntimeContext {
  session_id: string;
  workspace_instance_id: string;
  workspace_kind: string;
  checkpoint_rewind_released: boolean;
  managed_worktrees_released: boolean;
  worktree_creation_available?: boolean;
  worktree_creation_reason?: string | null;
  external_side_effects_reverted: false;
}

interface RuntimeCheckpoint {
  checkpoint_id: string;
  sequence: number;
  state: string;
  pin_state: string;
  has_irreversible_side_effects: boolean;
  external_side_effects: Array<Record<string, string>>;
  validation: RuntimeValidationSummary;
}

type RuntimeValidationStatus =
  | "not_requested"
  | "pass"
  | "fail"
  | "needs_review"
  | "failed_closed"
  | "cancelled"
  | "invalid";

interface RuntimeValidationSummary {
  overall_status: RuntimeValidationStatus;
  count: number;
  completed_count: number;
  failed_count: number;
  cancelled_count: number;
  verdict_counts: {
    pass: number;
    fail: number;
    needs_review: number;
  };
}

interface CheckpointList {
  checkpoints: RuntimeCheckpoint[];
  external_side_effects_are_reverted: false;
}

interface RewindPreview {
  target_checkpoint_id: string;
  paths: Array<{ relative_path: string; action: string }>;
  conflicts: Array<{ relative_path: string; reason: string }>;
  blockers: string[];
  can_execute: boolean;
  already_rewound: boolean;
  external_side_effects: Array<Record<string, string>>;
  external_side_effects_will_be_reverted: false;
}

interface WorktreeInspection {
  clean: boolean | null;
  registered: boolean;
  state: string;
  branch: string | null;
  already_released: boolean;
}

function runtimeErrorCode(error: unknown): string | null {
  if (!(error instanceof ApiError) || !error.body || typeof error.body !== "object") return null;
  const code = (error.body as { code?: unknown }).code;
  return typeof code === "string" ? code : null;
}

const validationBadges: Record<
  Exclude<RuntimeValidationStatus, "not_requested">,
  { label: string; className: string }
> = {
  pass: { label: "runtimeValidationPass", className: "border-emerald-500/25 bg-emerald-500/10 text-emerald-500" },
  fail: { label: "runtimeValidationFail", className: "border-red-500/25 bg-red-500/10 text-red-500" },
  needs_review: { label: "runtimeValidationNeedsReview", className: "border-amber-500/25 bg-amber-500/10 text-amber-500" },
  failed_closed: { label: "runtimeValidationFailedClosed", className: "border-orange-500/25 bg-orange-500/10 text-orange-500" },
  cancelled: { label: "runtimeValidationCancelled", className: "border-white/10 bg-white/[0.04] text-[var(--text-tertiary)]" },
  invalid: { label: "runtimeValidationInvalid", className: "border-red-500/25 bg-red-500/10 text-red-500" },
};

function validationBadge(
  status: RuntimeValidationStatus,
): { label: string; className: string } | null {
  return status === "not_requested" ? null : validationBadges[status];
}

function workspaceKindLabelKey(kind: string): string {
  switch (kind) {
    case "direct":
      return "runtimeWorkspaceKindDirect";
    case "git_worktree":
      return "runtimeWorkspaceKindIsolated";
    case "managed":
      return "runtimeWorkspaceKindManaged";
    default:
      return "runtimeWorkspaceKindUnknown";
  }
}

function checkpointStateLabelKey(state: string): string {
  switch (state) {
    case "prepared":
      return "runtimeCheckpointStatePrepared";
    case "committing":
      return "runtimeCheckpointStateSaving";
    case "finalized":
      return "runtimeCheckpointStateSaved";
    case "rewinding":
      return "runtimeCheckpointStateRestoring";
    case "rewound":
      return "runtimeCheckpointStateRestored";
    case "failed":
      return "runtimeCheckpointStateFailed";
    default:
      return "runtimeCheckpointStateUnknown";
  }
}

function worktreeUnavailableLabelKey(reason: string | null | undefined): string {
  switch (reason) {
    case "repository_not_supported":
      return "runtimeWorktreeUnavailableRepository";
    case "workspace_dirty":
      return "runtimeWorktreeUnavailableDirty";
    case "git_unavailable":
      return "runtimeWorktreeUnavailableGit";
    case "git_timeout":
      return "runtimeWorktreeUnavailableTimeout";
    case "storage_unavailable":
      return "runtimeWorktreeUnavailableStorage";
    case "workspace_not_supported":
      return "runtimeWorktreeUnavailableWorkspace";
    default:
      return "runtimeWorktreeUnavailableGeneric";
  }
}

function worktreeErrorLabelKey(code: string | null): string {
  switch (code) {
    case "worktree_repository_invalid":
      return "runtimeWorktreeErrorRepository";
    case "worktree_dirty":
      return "runtimeWorktreeErrorDirty";
    case "worktree_active":
      return "runtimeWorktreeErrorActive";
    case "worktree_ownership_mismatch":
    case "worktree_path_invalid":
    case "worktree_conflict":
      return "runtimeWorktreeErrorConflict";
    case "git_unavailable":
      return "runtimeWorktreeErrorGitUnavailable";
    case "git_timeout":
      return "runtimeWorktreeErrorTimeout";
    case "git_failed":
      return "runtimeWorktreeErrorGitFailed";
    case "runtime_audit_unavailable":
      return "runtimeWorktreeErrorAudit";
    default:
      return "runtimeWorktreeFailed";
  }
}

export function RuntimeControlCard({ sessionId }: { sessionId: string | null }) {
  const { t } = useTranslation("chat");
  const [context, setContext] = useState<RuntimeContext | null>(null);
  const [checkpoints, setCheckpoints] = useState<RuntimeCheckpoint[]>([]);
  const [worktree, setWorktree] = useState<WorktreeInspection | null>(null);
  const [collapsed, setCollapsed] = useState(true);
  const [loading, setLoading] = useState(false);
  const [busyAction, setBusyAction] = useState<string | null>(null);
  const [unavailable, setUnavailable] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);

  const load = useCallback(async (signal?: AbortSignal) => {
    if (!sessionId) {
      setContext(null);
      setUnavailable(true);
      setFailure(null);
      return;
    }
    setLoading(true);
    try {
      const nextContext = await api.get<RuntimeContext>(API.RUNTIME.CONTEXT(sessionId), { signal });
      setContext(nextContext);
      setUnavailable(false);
      setFailure(null);
      const [checkpointResult, worktreeResult] = await Promise.all([
        nextContext.checkpoint_rewind_released
          ? api.get<CheckpointList>(
              API.RUNTIME.CHECKPOINTS(sessionId, nextContext.workspace_instance_id),
              { signal },
            )
          : Promise.resolve({ checkpoints: [], external_side_effects_are_reverted: false as const }),
        nextContext.managed_worktrees_released && nextContext.workspace_kind === "git_worktree"
          ? api.get<WorktreeInspection>(
              API.RUNTIME.WORKTREE_INSPECT(sessionId, nextContext.workspace_instance_id),
              { signal },
            )
          : Promise.resolve(null),
      ]);
      setCheckpoints(checkpointResult.checkpoints);
      setWorktree(worktreeResult);
    } catch (error) {
      if (signal?.aborted) return;
      setContext(null);
      const code = runtimeErrorCode(error);
      const optionalSurfaceUnavailable =
        code === "v11_runtime_not_available" ||
        code === "runtime_workspace_not_found";
      setUnavailable(optionalSurfaceUnavailable);
      setFailure(
        optionalSurfaceUnavailable
          ? null
          : code === "runtime_workspace_provenance_mismatch"
            ? t("runtimeWorkspaceIdentityMismatch")
            : t("runtimeLoadFailed"),
      );
    } finally {
      if (!signal?.aborted) setLoading(false);
    }
  }, [sessionId, t]);

  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [load]);

  const reloadApplicationState = () => {
    useWorkspaceStore.getState().resetForSession();
    window.setTimeout(() => window.location.reload(), 100);
  };

  const rewind = async (checkpoint: RuntimeCheckpoint) => {
    if (!context || !sessionId) return;
    setBusyAction(checkpoint.checkpoint_id);
    try {
      const request = {
        session_id: sessionId,
        workspace_instance_id: context.workspace_instance_id,
        checkpoint_id: checkpoint.checkpoint_id,
      };
      const preview = await api.post<RewindPreview>(API.RUNTIME.REWIND_PREVIEW, request);
      if (!preview.can_execute) {
        toast.error(t("runtimeRewindBlocked"));
        return;
      }
      const warning = preview.external_side_effects.length > 0
        ? `\n\n${t("runtimeExternalEffectsWarning")}`
        : "";
      if (!window.confirm(t("runtimeRewindConfirm", { count: preview.paths.length }) + warning)) return;
      await api.post(API.RUNTIME.REWIND_EXECUTE, request);
      toast.success(t("runtimeRewindComplete"));
      reloadApplicationState();
    } catch {
      toast.error(t("runtimeRewindFailed"));
    } finally {
      setBusyAction(null);
    }
  };

  const createWorktree = async () => {
    if (!sessionId || !context) return;
    if (!window.confirm(t("runtimeWorktreeCreateConfirm"))) return;
    setBusyAction("worktree-create");
    try {
      await api.post(API.RUNTIME.WORKTREE_CREATE, { session_id: sessionId });
      toast.success(t("runtimeWorktreeCreated"));
      reloadApplicationState();
    } catch (error) {
      toast.error(t(worktreeErrorLabelKey(runtimeErrorCode(error))));
    } finally {
      setBusyAction(null);
    }
  };

  const releaseWorktree = async () => {
    if (!sessionId || !context) return;
    if (!window.confirm(t("runtimeWorktreeReleaseConfirm"))) return;
    setBusyAction("worktree-release");
    try {
      await api.post(API.RUNTIME.WORKTREE_RELEASE, {
        session_id: sessionId,
        workspace_instance_id: context.workspace_instance_id,
      });
      toast.success(t("runtimeWorktreeReleased"));
      reloadApplicationState();
    } catch (error) {
      toast.error(t(worktreeErrorLabelKey(runtimeErrorCode(error))));
    } finally {
      setBusyAction(null);
    }
  };

  if (!sessionId) return null;
  if (!context && loading) return null;
  if (unavailable && !loading) return null;
  if (failure && !context && !loading) {
    return (
      <div className="rounded-3xl border border-amber-500/30 bg-amber-500/5 p-4">
        <p className="flex items-center gap-2 text-[13px] font-medium text-[var(--text-primary)]">
          <ShieldAlert className="h-4 w-4 text-amber-500" />
          {t("runtimeControlTitle")}
        </p>
        <p className="mt-2 break-words text-[11px] text-[var(--text-secondary)]">{failure}</p>
        <Button className="mt-3" size="sm" variant="outline" onClick={() => void load()}>
          {t("retry")}
        </Button>
      </div>
    );
  }
  if (!context && !loading) return null;

  return (
    <div className="overflow-hidden rounded-3xl border border-white/8 bg-white/[0.03] shadow-[0_0_0_1px_rgba(255,255,255,0.02)_inset] backdrop-blur-sm">
      <button className="flex w-full items-start justify-between px-4 py-4 text-left transition-colors hover:bg-white/[0.02]" onClick={() => setCollapsed((value) => !value)}>
        <div className="flex min-w-0 flex-1 items-start gap-3">
          <div className="mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-2xl border border-white/8 bg-white/[0.04]">
            <History className="h-4 w-4 text-[var(--text-tertiary)]" />
          </div>
          <div className="min-w-0">
            <span className="block text-[13px] font-medium text-[var(--text-primary)]">{t("runtimeControlTitle")}</span>
            <span className="mt-1 block text-[12px] text-[var(--text-tertiary)]">
              {loading ? t("loading") : t("runtimeCheckpointCount", { count: checkpoints.length })}
            </span>
          </div>
        </div>
        <ChevronDown className={cn("mt-1 h-4 w-4 text-[var(--text-tertiary)] transition-transform", collapsed && "-rotate-90")} />
      </button>
      <AnimatePresence initial={false}>
        {!collapsed && context && (
          <motion.div initial={{ height: 0, opacity: 0 }} animate={{ height: "auto", opacity: 1 }} exit={{ height: 0, opacity: 0 }} className="overflow-hidden">
            <div className="space-y-3 border-t border-white/6 px-4 py-3">
              <div className="rounded-xl border border-emerald-500/15 bg-emerald-500/[0.04] px-3 py-2.5">
                <p className="flex items-center gap-2 text-xs font-medium text-[var(--text-primary)]">
                <BadgeCheck className="h-3.5 w-3.5 text-emerald-500" />
                  {t("runtimeProtectionReady")}
                </p>
                <p className="mt-1 pl-5.5 text-[11px] text-[var(--text-tertiary)]">
                  {t("runtimeProtectionReadyDesc")}
                </p>
              </div>

              {checkpoints.length === 0 ? (
                <p className="py-2 text-[12px] text-[var(--text-quaternary)]">{t("runtimeNoCheckpoints")}</p>
              ) : (
                <ul className="max-h-64 space-y-2 overflow-y-auto">
                  {checkpoints.slice(0, 20).map((checkpoint) => (
                    <li key={checkpoint.checkpoint_id} className="rounded-xl border border-white/8 p-2.5">
                      <div className="flex items-center justify-between gap-2">
                        <div className="min-w-0">
                          <p className="text-xs font-medium text-[var(--text-primary)]">{t("runtimeCheckpointLabel", { sequence: checkpoint.sequence })}</p>
                          <div className="mt-1 flex flex-wrap items-center gap-1.5">
                            <p className="text-[10px] text-[var(--text-tertiary)]">{t(checkpointStateLabelKey(checkpoint.state))}</p>
                            {(() => {
                              const badge = validationBadge(checkpoint.validation.overall_status);
                              return badge ? (
                                <span
                                  data-testid={`runtime-validation-${checkpoint.validation.overall_status}`}
                                  className={cn("rounded-full border px-1.5 py-0.5 text-[9px] font-medium", badge.className)}
                                >
                                  {t(badge.label)}
                                </span>
                              ) : null;
                            })()}
                          </div>
                        </div>
                        <Button size="sm" variant="ghost" disabled={busyAction !== null || checkpoint.state !== "finalized"} onClick={() => void rewind(checkpoint)}>
                          {busyAction === checkpoint.checkpoint_id ? <Loader2 className="animate-spin" /> : <RotateCcw />}
                          {t("runtimeRewind")}
                        </Button>
                      </div>
                      {checkpoint.has_irreversible_side_effects && (
                        <p className="mt-2 flex items-start gap-1.5 text-[10px] text-amber-500"><ShieldAlert className="mt-0.5 h-3 w-3 shrink-0" />{t("runtimeExternalEffectsWarning")}</p>
                      )}
                    </li>
                  ))}
                </ul>
              )}

              <details className="group rounded-xl border border-white/8">
                <summary className="flex cursor-pointer list-none items-center justify-between gap-3 px-3 py-2.5 [&::-webkit-details-marker]:hidden">
                  <div className="min-w-0">
                    <p className="text-xs font-medium text-[var(--text-primary)]">{t("runtimeAdvancedOptions")}</p>
                    <p className="mt-0.5 text-[10px] text-[var(--text-tertiary)]">{t("runtimeAdvancedOptionsDesc")}</p>
                  </div>
                  <ChevronDown className="h-3.5 w-3.5 shrink-0 text-[var(--text-tertiary)] transition-transform group-open:rotate-180" />
                </summary>
                <div className="space-y-2.5 border-t border-white/6 px-3 py-3">
                  {context.managed_worktrees_released && (
                    <div className="rounded-xl border border-white/8 p-2.5">
                      <div className="flex items-center justify-between gap-2">
                        <div className="min-w-0">
                          <p className="flex items-center gap-1.5 text-xs font-medium text-[var(--text-primary)]">
                            <GitBranch className="h-3.5 w-3.5" />
                            {t("runtimeWorktreeTitle")}
                            <span className="rounded-full bg-amber-500/10 px-1.5 py-0.5 text-[9px] font-normal text-amber-500">
                              {t("runtimePreviewFeature")}
                            </span>
                          </p>
                          <p className="mt-1 text-[11px] text-[var(--text-tertiary)]">
                            {context.workspace_kind === "git_worktree"
                              ? t("runtimeWorktreeActive")
                              : t("runtimeWorktreeDirect")}
                          </p>
                        </div>
                        {context.workspace_kind === "git_worktree" ? (
                          <Button size="sm" variant="outline" disabled={busyAction !== null || worktree?.clean === false} onClick={() => void releaseWorktree()}>
                            {busyAction === "worktree-release" ? <Loader2 className="animate-spin" /> : null}
                            {t("runtimeWorktreeRelease")}
                          </Button>
                        ) : (
                          <Button
                            size="sm"
                            variant="outline"
                            disabled={busyAction !== null || context.worktree_creation_available !== true}
                            onClick={() => void createWorktree()}
                          >
                            {busyAction === "worktree-create" ? <Loader2 className="animate-spin" /> : null}
                            {t("runtimeWorktreeCreate")}
                          </Button>
                        )}
                      </div>
                      {context.workspace_kind !== "git_worktree" && context.worktree_creation_available !== true && (
                        <p className="mt-2 text-[11px] text-amber-500">
                          {t(worktreeUnavailableLabelKey(context.worktree_creation_reason))}
                        </p>
                      )}
                      {worktree?.clean === false && <p className="mt-2 text-[11px] text-amber-500">{t("runtimeWorktreeDirty")}</p>}
                    </div>
                  )}

                  <details className="rounded-lg border border-white/8 px-2.5 py-2">
                    <summary className="cursor-pointer list-none text-[11px] font-medium text-[var(--text-secondary)] [&::-webkit-details-marker]:hidden">
                      {t("runtimeTechnicalDetails")}
                    </summary>
                    <dl className="mt-2 grid grid-cols-[auto_minmax(0,1fr)] gap-x-2 gap-y-1 text-[10px] text-[var(--text-tertiary)]">
                      <dt>{t("runtimeTechnicalWorkspaceType")}</dt>
                      <dd>{t(workspaceKindLabelKey(context.workspace_kind))}</dd>
                      <dt>{t("runtimeTechnicalWorkspaceId")}</dt>
                      <dd className="truncate font-mono" title={context.workspace_instance_id}>{context.workspace_instance_id.slice(0, 16)}</dd>
                      {worktree?.branch && (
                        <>
                          <dt>{t("runtimeTechnicalBranch")}</dt>
                          <dd className="truncate font-mono" title={worktree.branch}>{worktree.branch}</dd>
                        </>
                      )}
                    </dl>
                    {checkpoints.length > 0 && (
                      <ul className="mt-2 space-y-1 border-t border-white/6 pt-2 text-[10px] text-[var(--text-tertiary)]">
                        {checkpoints.slice(0, 20).map((checkpoint) => (
                          <li key={`technical-${checkpoint.checkpoint_id}`} className="flex items-center justify-between gap-2">
                            <span>{t("runtimeCheckpointLabel", { sequence: checkpoint.sequence })}</span>
                            <span className="truncate font-mono" title={checkpoint.checkpoint_id}>{checkpoint.checkpoint_id.slice(0, 12)}</span>
                          </li>
                        ))}
                      </ul>
                    )}
                  </details>
                </div>
              </details>
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}
