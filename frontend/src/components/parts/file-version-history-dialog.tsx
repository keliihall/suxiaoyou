"use client";

import { Clock3, Loader2, RotateCcw, ShieldCheck } from "lucide-react";
import { useTranslation } from "react-i18next";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { useFileVersions, useRestoreFileVersion } from "@/hooks/use-file-versions";
import { apiErrorMessage } from "@/lib/api";
import { formatFileVersionSize } from "@/lib/file-version";
import type { FileVersion } from "@/types/file-version";

interface FileVersionHistoryDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  sessionId: string;
  filePath: string;
  fileName: string;
}

function formatCreatedAt(value: string, locale: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat(locale, {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).format(date);
}

function VersionRow({
  version,
  locale,
  restoring,
  onRestore,
}: {
  version: FileVersion;
  locale: string;
  restoring: boolean;
  onRestore: () => void;
}) {
  const { t } = useTranslation("chat");
  return (
    <li className="rounded-xl border border-[var(--border-default)] bg-[var(--surface-secondary)] p-3">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0 space-y-1">
          <div className="flex items-center gap-1.5 text-sm font-medium text-[var(--text-primary)]">
            <Clock3 className="h-3.5 w-3.5" />
            <span>{formatCreatedAt(version.created_at, locale)}</span>
          </div>
          <p className="truncate text-xs text-[var(--text-secondary)]" title={version.operation}>
            {t("fileVersionOperation", { operation: version.operation })}
          </p>
          <p className="font-mono text-[11px] text-[var(--text-tertiary)]">
            {formatFileVersionSize(version.size)} · SHA-256 {version.sha256.slice(0, 12)}…
          </p>
        </div>
        <Button
          type="button"
          size="sm"
          variant="outline"
          disabled={restoring}
          onClick={onRestore}
        >
          {restoring ? <Loader2 className="animate-spin" /> : <RotateCcw />}
          {t("fileVersionRestore")}
        </Button>
      </div>
    </li>
  );
}

export function FileVersionHistoryDialog({
  open,
  onOpenChange,
  sessionId,
  filePath,
  fileName,
}: FileVersionHistoryDialogProps) {
  const { t, i18n } = useTranslation("chat");
  const versionsQuery = useFileVersions(sessionId, filePath, open);
  const restoreMutation = useRestoreFileVersion(sessionId, filePath);

  const restore = async (version: FileVersion) => {
    if (!window.confirm(t("fileVersionRestoreConfirm", { name: fileName }))) return;
    try {
      const result = await restoreMutation.mutateAsync(version.id);
      toast.success(
        result.recovery_version
          ? t("fileVersionRestoredWithRecovery")
          : t("fileVersionRestored"),
      );
    } catch (error) {
      toast.error(apiErrorMessage(error, t("fileVersionRestoreFailed")));
    }
  };

  const versions = versionsQuery.data?.versions ?? [];

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle>{t("fileVersionHistoryTitle", { name: fileName })}</DialogTitle>
          <DialogDescription>{t("fileVersionHistoryDescription")}</DialogDescription>
        </DialogHeader>

        <div className="flex items-start gap-2 rounded-lg border border-[var(--border-default)] bg-[var(--surface-tertiary)] p-3 text-xs text-[var(--text-secondary)]">
          <ShieldCheck className="mt-0.5 h-4 w-4 shrink-0 text-[var(--brand-primary)]" />
          <span>{t("fileVersionRecoveryNotice")}</span>
        </div>

        {versionsQuery.isLoading ? (
          <div className="flex min-h-32 items-center justify-center gap-2 text-sm text-[var(--text-secondary)]" aria-busy="true">
            <Loader2 className="animate-spin" />
            {t("fileVersionLoading")}
          </div>
        ) : versionsQuery.isError ? (
          <div className="space-y-3 rounded-lg border border-[var(--color-destructive)]/30 p-4 text-sm text-[var(--text-secondary)]">
            <p>{apiErrorMessage(versionsQuery.error, t("fileVersionLoadFailed"))}</p>
            <Button type="button" size="sm" variant="outline" onClick={() => void versionsQuery.refetch()}>
              {t("retry")}
            </Button>
          </div>
        ) : versions.length === 0 ? (
          <div className="rounded-lg border border-dashed border-[var(--border-default)] p-6 text-center text-sm text-[var(--text-secondary)]">
            {t("fileVersionEmpty")}
          </div>
        ) : (
          <ul className="max-h-[55vh] space-y-2 overflow-y-auto pr-1" data-testid="file-version-list">
            {versions.map((version) => (
              <VersionRow
                key={version.id}
                version={version}
                locale={i18n.language}
                restoring={restoreMutation.isPending}
                onRestore={() => void restore(version)}
              />
            ))}
          </ul>
        )}
      </DialogContent>
    </Dialog>
  );
}
