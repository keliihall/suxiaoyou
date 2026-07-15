"use client";

import { useEffect, useMemo, useRef, useState } from "react";
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
import { ApiError, apiErrorMessage } from "@/lib/api";
import { goalBudgetMaximumFromError } from "@/lib/goal-ui";
import type { GoalUpdateRequest, SessionGoal } from "@/types/goal";

const GOAL_TEXT_LIMIT = 4_000;

interface GoalEditorDialogProps {
  goal: SessionGoal;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onSave: (
    update: Omit<GoalUpdateRequest, "client_request_id">,
  ) => Promise<unknown>;
  maxTokenBudget?: number | null;
}

export function GoalEditorDialog({
  goal,
  open,
  onOpenChange,
  onSave,
  maxTokenBudget,
}: GoalEditorDialogProps) {
  const { t, i18n } = useTranslation("chat");
  const [objective, setObjective] = useState(goal.objective);
  const [definitionOfDone, setDefinitionOfDone] = useState(goal.definition_of_done ?? "");
  const [tokenBudget, setTokenBudget] = useState(
    goal.token_budget == null ? "" : String(goal.token_budget),
  );
  const [baseRevision, setBaseRevision] = useState(goal.revision);
  const [saving, setSaving] = useState(false);
  const wasOpen = useRef(false);

  useEffect(() => {
    const opening = open && !wasOpen.current;
    wasOpen.current = open;
    if (opening) {
      setObjective(goal.objective);
      setDefinitionOfDone(goal.definition_of_done ?? "");
      setTokenBudget(goal.token_budget == null ? "" : String(goal.token_budget));
      setBaseRevision(goal.revision);
    }
  }, [goal.definition_of_done, goal.objective, goal.revision, goal.token_budget, open]);

  const textLength = Array.from(objective).length + Array.from(definitionOfDone).length;
  const parsedTokenBudget = tokenBudget.trim() === "" ? null : Number(tokenBudget);
  const exceedsTokenBudgetMaximum = parsedTokenBudget !== null
    && maxTokenBudget != null
    && parsedTokenBudget > maxTokenBudget;
  const validTokenBudget = parsedTokenBudget === null
    || (
      Number.isSafeInteger(parsedTokenBudget)
      && parsedTokenBudget >= 0
      && !exceedsTokenBudgetMaximum
    );
  const canSave = objective.trim().length > 0
    && textLength <= GOAL_TEXT_LIMIT
    && validTokenBudget
    && !saving;
  const countClass = useMemo(
    () => textLength > GOAL_TEXT_LIMIT
      ? "text-[var(--color-destructive)]"
      : textLength >= GOAL_TEXT_LIMIT * 0.9
        ? "text-[var(--color-warning)]"
        : "text-[var(--text-tertiary)]",
    [textLength],
  );
  const numberLanguage = i18n.resolvedLanguage || i18n.language || "en";
  const formatTokens = (value: number) => new Intl.NumberFormat(numberLanguage).format(value);

  const handleSave = async () => {
    if (!canSave) return;
    setSaving(true);
    try {
      await onSave({
        expected_revision: baseRevision,
        objective: objective.trim(),
        definition_of_done: definitionOfDone.trim() || null,
        token_budget: parsedTokenBudget,
      });
      toast.success(t("goalUpdated"));
      onOpenChange(false);
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) {
        toast.error(t("goalRevisionConflict"));
        onOpenChange(false);
      } else {
        const serverMaximum = goalBudgetMaximumFromError(error);
        toast.error(serverMaximum == null
          ? apiErrorMessage(error, t("goalUpdateFailed"))
          : t("goalTokenBudgetMaximumError", {
            maximum: formatTokens(serverMaximum),
          }));
      }
    } finally {
      setSaving(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-xl">
        <DialogHeader>
          <DialogTitle>{t("goalEditTitle")}</DialogTitle>
          <DialogDescription>{t("goalEditDescription")}</DialogDescription>
        </DialogHeader>

        <div className="space-y-4">
          <label className="block space-y-1.5">
            <span className="text-xs font-medium text-[var(--text-primary)]">{t("goalObjective")}</span>
            <textarea
              autoFocus
              value={objective}
              onChange={(event) => setObjective(event.target.value)}
              rows={5}
              className="w-full resize-y rounded-xl border border-[var(--border-default)] bg-[var(--surface-secondary)] px-3 py-2.5 text-sm leading-relaxed text-[var(--text-primary)] outline-none placeholder:text-[var(--text-tertiary)] focus:border-[var(--border-heavy)]"
              placeholder={t("goalObjectivePlaceholder")}
            />
          </label>

          <label className="block space-y-1.5">
            <span className="text-xs font-medium text-[var(--text-primary)]">{t("goalDefinitionOfDone")}</span>
            <textarea
              value={definitionOfDone}
              onChange={(event) => setDefinitionOfDone(event.target.value)}
              rows={3}
              className="w-full resize-y rounded-xl border border-[var(--border-default)] bg-[var(--surface-secondary)] px-3 py-2.5 text-sm leading-relaxed text-[var(--text-primary)] outline-none placeholder:text-[var(--text-tertiary)] focus:border-[var(--border-heavy)]"
              placeholder={t("goalDefinitionOfDonePlaceholder")}
            />
          </label>

          <div className="grid gap-3 sm:grid-cols-[1fr_auto] sm:items-end">
            <label className="block space-y-1.5">
              <span className="text-xs font-medium text-[var(--text-primary)]">{t("goalTokenBudget")}</span>
              <input
                type="number"
                min={0}
                max={maxTokenBudget ?? undefined}
                step={1}
                inputMode="numeric"
                value={tokenBudget}
                onChange={(event) => setTokenBudget(event.target.value)}
                className="h-10 w-full rounded-xl border border-[var(--border-default)] bg-[var(--surface-secondary)] px-3 text-sm text-[var(--text-primary)] outline-none focus:border-[var(--border-heavy)]"
                placeholder={t("goalTokenBudgetDefault")}
              />
            </label>
            <span className={countClass}>{t("goalCharacterCount", { count: textLength, limit: GOAL_TEXT_LIMIT })}</span>
          </div>
          <p className="text-xs leading-relaxed text-[var(--text-tertiary)]">
            {maxTokenBudget == null
              ? t("goalTokenBudgetUsedHint", { used: formatTokens(goal.tokens_used) })
              : t("goalTokenBudgetHint", {
                maximum: formatTokens(maxTokenBudget),
                used: formatTokens(goal.tokens_used),
              })}
          </p>
          {!validTokenBudget && (
            <p role="alert" className="text-xs text-[var(--color-destructive)]">
              {exceedsTokenBudgetMaximum && maxTokenBudget != null
                ? t("goalTokenBudgetMaximumError", {
                  maximum: formatTokens(maxTokenBudget),
                })
                : t("goalTokenBudgetInvalid")}
            </p>
          )}
        </div>

        <div className="flex justify-end gap-2 pt-1">
          <Button variant="outline" onClick={() => onOpenChange(false)} disabled={saving}>
            {t("cancel")}
          </Button>
          <Button onClick={() => void handleSave()} disabled={!canSave}>
            {saving ? t("goalSaving") : t("goalSave")}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
