import type { TFunction } from "i18next";

type PluginTranslator = TFunction<"plugins">;

/**
 * Catalog values come from backend manifests and must remain stable there.
 * Translate only their presentation; unknown/custom values deliberately fall
 * back to the original text so user-authored metadata is never rewritten.
 */
function catalogToken(value: string): string {
  return value.replaceAll(":", "__").replaceAll(".", "_dot_");
}

function catalogText(
  t: PluginTranslator,
  section: "connectors" | "plugins" | "skills",
  id: string,
  field: "name" | "description",
  fallback: string,
): string {
  return t(`catalog.${section}.${catalogToken(id)}.${field}`, {
    defaultValue: fallback,
  });
}

export function localizeConnectorName(
  t: PluginTranslator,
  id: string,
  fallback: string,
): string {
  return catalogText(t, "connectors", id, "name", fallback);
}

export function localizeConnectorDescription(
  t: PluginTranslator,
  id: string,
  fallback: string,
): string {
  return catalogText(t, "connectors", id, "description", fallback);
}

export function localizePluginName(
  t: PluginTranslator,
  id: string,
  fallback = id,
): string {
  return catalogText(t, "plugins", id, "name", fallback);
}

export function localizePluginDescription(
  t: PluginTranslator,
  id: string,
  fallback: string,
): string {
  return catalogText(t, "plugins", id, "description", fallback);
}

export function localizeSkillName(
  t: PluginTranslator,
  id: string,
  fallback = id,
): string {
  return catalogText(t, "skills", id, "name", fallback);
}

export function localizeSkillDescription(
  t: PluginTranslator,
  id: string,
  fallback: string,
): string {
  return catalogText(t, "skills", id, "description", fallback);
}
