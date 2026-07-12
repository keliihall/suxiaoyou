"use client";

import { useState, useMemo, useEffect } from "react";
import { useRouter } from "next/navigation";
import { useTranslation } from "react-i18next";
import { AlertCircle, Check, ChevronDown, Loader2 } from "lucide-react";
import { useProviderModels } from "@/hooks/use-provider-models";
import { useSettingsStore } from "@/stores/settings-store";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import {
  Command,
  CommandInput,
  CommandList,
  CommandEmpty,
  CommandGroup,
  CommandItem,
} from "@/components/ui/command";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";
import type { ModelInfo } from "@/types/model";

function isLegacyFreeRouterModel(m: ModelInfo): boolean {
  const normalizedName = m.name.trim().toLowerCase();
  return m.id === "openrouter/auto" || normalizedName === "free models router";
}

/**
 * Variant keywords that disambiguate providers with explicit tier naming
 * (Fast / Heavy / Heavy Reasoning, etc.). Order matters: longer multi-word
 * variants are matched before single-word ones.
 *
 * Intentionally narrow — we only run variant detection for providers where
 * we own the naming scheme. Third-party brand names like "GLM 4.5 Air",
 * "Gemini 2.5 Flash", "Phi-3 Mini" use these words as part of the family
 * identity rather than as a tier, and substring matching there would split
 * legitimate brand names and collide otherwise-distinct models in the UI.
 */
const VARIANT_KEYWORDS = ["Heavy Reasoning", "Fast Reasoning", "Heavy", "Fast"];

/** Providers whose model names use the 苏小有 Fast/Heavy variant scheme. */
const VARIANT_AWARE_PROVIDERS = new Set<string | null>([]);

/**
 * Splits a model display name into {family, variant}. Only attempts variant
 * detection when the active provider is known to use the Fast/Heavy scheme;
 * otherwise returns the trimmed name as family with no variant.
 */
function splitModelDisplayName(
  name: string,
  provider: string | null,
): { family: string; variant: string | null } {
  const trimmed = name.trim();
  if (!VARIANT_AWARE_PROVIDERS.has(provider)) {
    return { family: trimmed, variant: null };
  }
  for (const kw of VARIANT_KEYWORDS) {
    // Match keyword as a trailing token (case-insensitive, optional separators).
    const re = new RegExp(`[\\s\\-_·]+${kw}\\s*$`, "i");
    const match = trimmed.match(re);
    if (match) {
      const family = trimmed.slice(0, match.index).trim();
      if (family.length > 0) {
        return { family, variant: kw };
      }
    }
  }
  return { family: trimmed, variant: null };
}

function preserveModelSuffix(name: string, max = 42): string {
  if (name.length <= max) return name;
  const head = Math.max(12, Math.floor(max * 0.55));
  const tail = Math.max(12, max - head - 1);
  return `${name.slice(0, head).trimEnd()}…${name.slice(-tail).trimStart()}`;
}

export function HeaderModelDropdown({ compact = false }: { compact?: boolean }) {
  const { t } = useTranslation("common");
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const {
    data: models,
    isLoading,
    isError,
    activeProvider,
  } = useProviderModels();
  const { selectedModel, selectedProviderId, setSelectedModel } =
    useSettingsStore();
  const noModels = !activeProvider || (models ?? []).length === 0;
  const visibleModels = useMemo(
    () => (models ?? []).filter((m) => !isLegacyFreeRouterModel(m)),
    [models],
  );

  // Auto-select a sensible default when no model is selected or current model doesn't exist in the active provider
  useEffect(() => {
    if (visibleModels.length === 0) {
      if (selectedModel) setSelectedModel(null);
      return;
    }
    const modelExists =
      selectedModel &&
      visibleModels.some(
        (m) => m.id === selectedModel && m.provider_id === selectedProviderId,
      );
    if (!modelExists) {
      let chosen: ModelInfo;
      if (activeProvider === "chatgpt") {
        // Prefer the newest flagship (5.5), fall back to 5.4 if the user's
        // subscription tier hasn't rolled it out yet, then to whatever the
        // backend did return.
        const preferred =
          visibleModels.find((m) => m.id === "openai-subscription/gpt-5.5") ??
          visibleModels.find((m) => m.id === "openai-subscription/gpt-5.4");
        chosen = preferred ?? visibleModels[0];
      } else {
        chosen = visibleModels[0];
      }
      setSelectedModel(chosen.id, chosen.provider_id);
    }
  }, [
    visibleModels,
    selectedModel,
    selectedProviderId,
    setSelectedModel,
    activeProvider,
  ]);

  const sortedModels = useMemo(
    () => [...visibleModels].sort((a, b) =>
      b.name.localeCompare(a.name, undefined, { numeric: true }),
    ),
    [visibleModels],
  );

  const selectedInfo =
    visibleModels.find(
      (m) => m.id === selectedModel && m.provider_id === selectedProviderId,
    ) ?? visibleModels.find((m) => m.id === selectedModel);
  const selectedLabel =
    selectedInfo?.name ??
    (selectedModel
      ? selectedModel.includes("/")
        ? (selectedModel.split("/").pop() ?? selectedModel)
        : selectedModel
      : t("noModelFound"));
  const shortModel = preserveModelSuffix(selectedLabel);

  // Models still loading with an active provider — show loading indicator
  if (isLoading && activeProvider) {
    return (
      <button
        type="button"
        disabled
        className={cn(
          "inline-flex h-7 items-center gap-1.5 rounded-lg border-none bg-transparent font-semibold text-[var(--text-tertiary)] shadow-none focus:outline-none cursor-default",
          compact ? "max-w-[160px] px-2 text-[12px]" : "max-w-[220px] px-3 text-[13px]",
        )}
      >
        <Loader2 className="h-4 w-4 animate-spin shrink-0" />
        <span className="truncate">
          {t("loadingModels", "Loading models...")}
        </span>
      </button>
    );
  }

  if (isError && activeProvider) {
    return (
      <TooltipProvider>
        <Tooltip>
          <TooltipTrigger asChild>
            <button
              type="button"
              onClick={() => router.push("/settings?tab=providers")}
              className={cn(
                "inline-flex h-7 items-center gap-1.5 rounded-lg border-none bg-transparent font-semibold text-[var(--text-secondary)] shadow-none transition-colors hover:bg-[var(--surface-secondary)] focus:outline-none cursor-pointer",
                compact ? "max-w-[160px] px-2 text-[12px]" : "max-w-[220px] px-3 text-[13px]",
              )}
            >
              <AlertCircle className="h-4 w-4 shrink-0 text-[var(--color-destructive)]" />
              <span className="truncate">
                {t("modelsUnavailable", "Models unavailable")}
              </span>
            </button>
          </TooltipTrigger>
          <TooltipContent>
            <p>
              {t(
                "modelsUnavailableHint",
                "Check your provider connection, firewall, or VPN settings.",
              )}
            </p>
          </TooltipContent>
        </Tooltip>
      </TooltipProvider>
    );
  }

  // No models available — clicking navigates to provider settings instead of opening dropdown
  if (noModels) {
    return (
      <button
        type="button"
        onClick={() => router.push("/settings?tab=providers")}
        className={cn(
          "inline-flex h-7 items-center gap-1.5 rounded-lg border-none bg-transparent font-semibold text-[var(--text-secondary)] shadow-none transition-colors hover:bg-[var(--surface-secondary)] focus:outline-none cursor-pointer",
          compact ? "max-w-[160px] px-2 text-[12px]" : "max-w-[220px] px-3 text-[13px]",
        )}
      >
        <span className="truncate">{t("setupProvider")}</span>
        <ChevronDown className="h-4 w-4 opacity-50 shrink-0" />
      </button>
    );
  }

  const { family: modelFamily, variant: modelVariant } = splitModelDisplayName(
    selectedLabel,
    activeProvider,
  );

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <button
          type="button"
          aria-label={
            modelVariant ? `${modelFamily} (${modelVariant})` : modelFamily
          }
          title={selectedLabel}
          className={cn(
            "inline-flex translate-y-[1px] items-center gap-1.5 rounded-lg border-none bg-transparent px-3 shadow-none transition-colors hover:bg-[var(--surface-secondary)] focus:outline-none cursor-pointer",
            // Two-line layout when a variant is detected; otherwise keep single
            // line so the visual matches the existing dropdown trigger height.
            compact
              ? "h-7 max-w-[160px] px-2 text-[12px] font-semibold text-[var(--text-primary)]"
              : modelVariant
                ? "h-10 max-w-[320px] sm:max-w-[420px] py-1"
                : "h-7 max-w-[320px] sm:max-w-[420px] text-[13px] font-semibold text-[var(--text-primary)]",
          )}
        >
          {modelVariant && !compact ? (
            <span className="flex flex-col items-start min-w-0 leading-tight">
              <span className="truncate max-w-full text-[13px] font-semibold text-[var(--text-primary)]">
                {modelFamily}
              </span>
              <span className="truncate max-w-full text-[10px] font-medium uppercase tracking-[0.08em] text-[var(--text-tertiary)]">
                {modelVariant}
              </span>
            </span>
          ) : (
            <span className="truncate">{shortModel}</span>
          )}
          <ChevronDown className="h-4 w-4 opacity-50 shrink-0" />
        </button>
      </PopoverTrigger>
      <PopoverContent
        className="w-[min(320px,calc(100vw-24px))] p-0 overflow-hidden"
        align={compact ? "end" : "start"}
        sideOffset={4}
      >
        <TooltipProvider delayDuration={300}>
          <Command>
            <CommandInput placeholder={t("searchModels")} />
            <CommandList>
              <CommandEmpty>{t("noModelFound")}</CommandEmpty>

              {isLoading ? (
                <div className="px-3 py-2">
                  <div className="h-5 rounded-md bg-[var(--surface-tertiary)] animate-pulse" />
                </div>
              ) : (
                <CommandGroup>
                  {sortedModels.map((model) => (
                    <ModelRow
                      key={`${model.provider_id}/${model.id}`}
                      model={model}
                      isSelected={
                        selectedModel === model.id &&
                        selectedProviderId === model.provider_id
                      }
                      onSelect={() => {
                        setSelectedModel(model.id, model.provider_id);
                        setOpen(false);
                      }}
                    />
                  ))}
                </CommandGroup>
              )}
            </CommandList>
          </Command>
        </TooltipProvider>
      </PopoverContent>
    </Popover>
  );
}

function ModelRow({
  model,
  isSelected,
  onSelect,
}: {
  model: ModelInfo;
  isSelected: boolean;
  onSelect: () => void;
}) {
  return (
    <CommandItem
      value={model.name}
      onSelect={onSelect}
      className="text-sm"
      title={model.name}
    >
      <Check
        className={cn(
          "mr-2 h-4 w-4 shrink-0",
          isSelected ? "opacity-100" : "opacity-0",
        )}
      />
      <span className="min-w-0 flex-1 truncate">{model.name}</span>
    </CommandItem>
  );
}
