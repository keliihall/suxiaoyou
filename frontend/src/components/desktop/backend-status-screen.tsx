"use client";

import { FolderOpen, LoaderCircle, RefreshCw, TriangleAlert } from "lucide-react";
import { useTranslation } from "react-i18next";
import type { BackendStatus } from "@/lib/backend-lifecycle";
import { Button } from "@/components/ui/button";
import { SuxiaoyouLogo } from "@/components/ui/suxiaoyou-logo";

interface BackendStatusScreenProps {
  status: BackendStatus;
  relaunching?: boolean;
  actionError?: string | null;
  onRelaunch?: () => void;
  onOpenLogs?: () => void;
}

export function BackendStatusScreen({
  status,
  relaunching = false,
  actionError,
  onRelaunch,
  onOpenLogs,
}: BackendStatusScreenProps) {
  const { t } = useTranslation("common");

  if (status.phase === "ready") return null;

  const failed = status.phase === "failed";
  const restarting = status.phase === "restarting";
  const hasRestartProgress =
    restarting &&
    typeof status.attempt === "number" &&
    typeof status.max_attempts === "number";

  const title = failed
    ? t("backendFailedTitle")
    : restarting
      ? t("backendRestartingTitle")
      : t("backendInitializingTitle");
  const description = failed
    ? t("backendFailedDescription")
    : hasRestartProgress
      ? t("backendRestartingProgress", {
          attempt: status.attempt,
          max: status.max_attempts,
        })
      : restarting
        ? t("backendRestartingDescription")
        : t("backendInitializingDescription");

  return (
    <div
      className="fixed inset-0 z-[9998] flex items-center justify-center bg-[var(--surface-primary)] px-6 text-[var(--text-primary)]"
      role={failed ? "alertdialog" : "status"}
      aria-modal={failed ? true : undefined}
      aria-labelledby="backend-status-title"
      aria-describedby="backend-status-description"
      aria-live={failed ? "assertive" : "polite"}
      aria-busy={!failed}
    >
      <div className="flex w-full max-w-md flex-col items-center text-center">
        <div className="relative mb-6 flex h-20 w-20 items-center justify-center rounded-3xl border border-[var(--border-default)] bg-[var(--surface-secondary)] shadow-[var(--shadow-md)]">
          <SuxiaoyouLogo size={44} />
          <span className="absolute -bottom-1 -right-1 flex h-7 w-7 items-center justify-center rounded-full border border-[var(--border-default)] bg-[var(--surface-primary)]">
            {failed ? (
              <TriangleAlert className="h-4 w-4 text-[var(--color-destructive)]" />
            ) : (
              <LoaderCircle className="h-4 w-4 animate-spin text-[var(--brand-primary)]" />
            )}
          </span>
        </div>

        <h1 id="backend-status-title" className="text-lg font-semibold tracking-tight">
          {title}
        </h1>
        <p
          id="backend-status-description"
          className="mt-2 max-w-sm text-sm leading-6 text-[var(--text-secondary)]"
        >
          {description}
        </p>

        {failed && (
          <>
            <div className="mt-6 flex flex-wrap items-center justify-center gap-3">
              <Button
                type="button"
                onClick={onRelaunch}
                disabled={relaunching || !onRelaunch}
                autoFocus
              >
                <RefreshCw className={relaunching ? "animate-spin" : undefined} />
                {relaunching ? t("backendRelaunching") : t("backendRetry")}
              </Button>
              <Button
                type="button"
                variant="outline"
                onClick={onOpenLogs}
                disabled={!onOpenLogs}
              >
                <FolderOpen />
                {t("backendOpenLogs")}
              </Button>
            </div>

            {actionError && (
              <p className="mt-4 max-w-sm text-xs leading-5 text-[var(--color-destructive)]">
                {actionError.slice(0, 500)}
              </p>
            )}

            {(status.failure_code || status.detail) && (
              <details className="mt-6 w-full rounded-lg border border-[var(--border-default)] bg-[var(--surface-secondary)] px-4 py-3 text-left">
                <summary className="cursor-pointer select-none text-xs font-medium text-[var(--text-secondary)]">
                  {t("backendTechnicalDetails")}
                </summary>
                <pre className="mt-3 max-h-40 overflow-auto whitespace-pre-wrap break-words font-mono text-[11px] leading-5 text-[var(--text-tertiary)]">
                  {[status.failure_code, status.detail]
                    .filter(Boolean)
                    .join("\n\n")
                    .slice(0, 2000)}
                </pre>
              </details>
            )}
          </>
        )}
      </div>
    </div>
  );
}
