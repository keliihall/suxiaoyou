export type VisibleToolMetadataKey =
  | "provider"
  | "model"
  | "estimatedCost"
  | "actualCost"
  | "costNotice";

export interface VisibleToolMetadataItem {
  key: VisibleToolMetadataKey;
  value: string;
  title?: string;
  warning?: boolean;
}

function boundedString(value: unknown): string | null {
  if (typeof value !== "string") return null;
  const normalized = value.trim();
  return normalized ? normalized.slice(0, 240) : null;
}

function finiteNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) && value >= 0
    ? value
    : null;
}

function providerName(metadata: Record<string, unknown>): string | null {
  const explicit = boundedString(metadata.provider_name);
  if (explicit) return explicit;

  const provider = boundedString(metadata.provider);
  if (!provider) return null;
  if (provider.toLowerCase() === "siliconflow") return "SiliconFlow";
  return provider;
}

function formatCost(
  amount: number,
  currency: string,
  unit: string | null,
  language: string,
): string {
  let formatted: string;
  try {
    formatted = new Intl.NumberFormat(language.startsWith("zh") ? "zh-CN" : "en-US", {
      style: "currency",
      currency,
      currencyDisplay: "narrowSymbol",
      minimumFractionDigits: 2,
      maximumFractionDigits: 4,
    }).format(amount);
  } catch {
    formatted = `${currency} ${amount.toFixed(4)}`;
  }

  if (unit === "image") {
    return `${formatted}/${language.startsWith("zh") ? "张" : "image"}`;
  }
  return unit ? `${formatted}/${unit}` : formatted;
}

/**
 * Select the small, user-relevant subset of tool metadata safe to show in UI.
 * Actual cost wins over an estimate; a numeric zero remains visible.
 */
export function getVisibleToolMetadata(
  metadata: Record<string, unknown> | null | undefined,
  language = "en",
): VisibleToolMetadataItem[] {
  if (!metadata) return [];

  const items: VisibleToolMetadataItem[] = [];
  const provider = providerName(metadata);
  const model = boundedString(metadata.model);
  if (provider) items.push({ key: "provider", value: provider });
  if (model) items.push({ key: "model", value: model });

  const actualCost = finiteNumber(metadata.actual_cost);
  const estimatedCost = finiteNumber(metadata.estimated_cost);
  const currency = boundedString(metadata.currency) ?? "USD";
  const unit = boundedString(metadata.pricing_unit);
  const costNotice = boundedString(metadata.cost_notice);
  const pricingAsOf = boundedString(metadata.pricing_as_of);
  const costTitle = [costNotice, pricingAsOf ? `Pricing as of ${pricingAsOf}` : null]
    .filter(Boolean)
    .join(" · ");

  if (actualCost !== null) {
    items.push({
      key: "actualCost",
      value: formatCost(actualCost, currency, unit, language),
      title: costTitle || undefined,
    });
  } else if (estimatedCost !== null) {
    items.push({
      key: "estimatedCost",
      value: formatCost(estimatedCost, currency, unit, language),
      title: costTitle || undefined,
      warning: true,
    });
  } else if (costNotice) {
    items.push({ key: "costNotice", value: costNotice, warning: true });
  }

  return items;
}
