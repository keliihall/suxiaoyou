export const DESKTOP_PERMISSION_SOURCE = "desktop" as const;

export type SavedPermissionSource = typeof DESKTOP_PERMISSION_SOURCE;

/**
 * A remembered decision is reusable only inside the scope in which the user
 * approved it. Workspace-backed conversations share a workspace scope;
 * folderless conversations fall back to one session scope.
 */
export interface SavedPermissionRule {
  tool: string;
  allow: boolean;
  pattern: string;
  workspace: string | null;
  sessionId: string | null;
  source: SavedPermissionSource;
  timestamp: number;
}

export interface SavedPermissionContext {
  workspace: string | null;
  sessionId: string | null;
  source: SavedPermissionSource;
}

export interface SavedPermissionRuleInput extends SavedPermissionContext {
  tool: string;
  allow: boolean;
  pattern: string;
}

export interface PermissionRulePayload {
  action: "allow" | "deny";
  permission: string;
  pattern: string;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

export function normalizePermissionWorkspace(
  workspace: string | null | undefined,
): string | null {
  if (typeof workspace !== "string") return null;
  const normalized = workspace.trim();
  return normalized && normalized !== "." ? normalized : null;
}

function normalizeSessionId(sessionId: unknown): string | null {
  if (typeof sessionId !== "string") return null;
  const normalized = sessionId.trim();
  return normalized || null;
}

function normalizePattern(pattern: unknown): string | null {
  if (typeof pattern !== "string" || !pattern.trim()) return null;
  // Preserve the backend-provided bytes. Trimming a command or path pattern
  // here could silently change the permission that the user approved.
  return pattern;
}

function normalizeTool(tool: unknown): string | null {
  if (typeof tool !== "string") return null;
  const normalized = tool.trim();
  return normalized || null;
}

export function savedPermissionRuleKey(
  rule: Pick<
    SavedPermissionRule,
    "source" | "workspace" | "sessionId" | "tool" | "pattern"
  >,
): string {
  return JSON.stringify([
    rule.source,
    rule.workspace,
    rule.sessionId,
    rule.tool,
    rule.pattern,
  ]);
}

function normalizeRule(
  value: unknown,
  fallbackWorkspace: string | null,
  timestampFallback?: number,
): SavedPermissionRule | null {
  if (!isRecord(value)) return null;

  const tool = normalizeTool(value.tool);
  const pattern = normalizePattern(value.pattern);
  if (!tool || !pattern || typeof value.allow !== "boolean") {
    // v4 and earlier stored only {tool, allow}. There is no safe way to
    // reconstruct the approved pattern, so those rules must be confirmed
    // again instead of being upgraded to a wildcard.
    return null;
  }

  const source = value.source ?? DESKTOP_PERMISSION_SOURCE;
  if (source !== DESKTOP_PERMISSION_SOURCE) return null;

  const hasWorkspaceField = Object.prototype.hasOwnProperty.call(
    value,
    "workspace",
  );
  const workspace = hasWorkspaceField
    ? normalizePermissionWorkspace(value.workspace as string | null | undefined)
    : fallbackWorkspace;
  const sessionId = normalizeSessionId(value.sessionId);
  if (!workspace && !sessionId) return null;

  const timestamp =
    typeof value.timestamp === "number" && Number.isFinite(value.timestamp)
      ? value.timestamp
      : timestampFallback;
  if (timestamp === undefined) return null;

  return {
    tool,
    allow: value.allow,
    pattern,
    workspace,
    // A workspace scope is intentionally reusable across sessions. Keep only
    // the narrower session fallback when no workspace is available.
    sessionId: workspace ? null : sessionId,
    source: DESKTOP_PERMISSION_SOURCE,
    timestamp,
  };
}

/**
 * Safely migrate persisted data. Tool-only legacy decisions are removed;
 * explicit-pattern rules are retained only when they can be bound to a
 * workspace or session.
 */
export function migrateSavedPermissionRules(
  value: unknown,
  fallbackWorkspace?: string | null,
): SavedPermissionRule[] {
  if (!Array.isArray(value)) return [];
  const workspace = normalizePermissionWorkspace(fallbackWorkspace);
  const byScope = new Map<string, SavedPermissionRule>();

  for (const candidate of value) {
    const rule = normalizeRule(candidate, workspace);
    if (!rule) continue;
    const key = savedPermissionRuleKey(rule);
    const previous = byScope.get(key);
    if (!previous || previous.timestamp <= rule.timestamp) {
      byScope.set(key, rule);
    }
  }

  return [...byScope.values()];
}

export function upsertSavedPermissionRule(
  rules: SavedPermissionRule[],
  input: SavedPermissionRuleInput,
  timestamp = Date.now(),
): SavedPermissionRule[] {
  const next = normalizeRule(
    { ...input, timestamp },
    normalizePermissionWorkspace(input.workspace),
    timestamp,
  );
  if (!next) return rules;

  const key = savedPermissionRuleKey(next);
  return [
    ...rules.filter((rule) => savedPermissionRuleKey(rule) !== key),
    next,
  ];
}

export function getSavedPermissionDecision(
  rules: SavedPermissionRule[],
  tool: string,
  pattern: string,
  context: SavedPermissionContext,
): boolean | null {
  const applicable = savedPermissionRulesForContext(rules, context);
  const rule = applicable.find(
    (candidate) => candidate.permission === tool && candidate.pattern === pattern,
  );
  return rule ? rule.action === "allow" : null;
}

/** Build the exact backend rules for one invocation context. */
export function savedPermissionRulesForContext(
  rules: SavedPermissionRule[],
  context: SavedPermissionContext,
): PermissionRulePayload[] {
  const workspace = normalizePermissionWorkspace(context.workspace);
  const sessionId = normalizeSessionId(context.sessionId);
  const applicable: SavedPermissionRule[] = [];

  for (const candidate of rules) {
    const rule = normalizeRule(candidate, null);
    if (!rule || rule.source !== context.source) continue;

    const inScope = rule.workspace
      ? rule.workspace === workspace
      : Boolean(rule.sessionId && rule.sessionId === sessionId);
    if (inScope) applicable.push(rule);
  }

  // A rule may first be remembered against one conversation while session
  // metadata is loading, then later against its workspace. Emit only one
  // decision per tool/pattern, with the narrower conversation rule winning.
  applicable.sort((left, right) => Number(!left.workspace) - Number(!right.workspace));
  const byPermissionPattern = new Map<string, PermissionRulePayload>();
  for (const rule of applicable) {
    byPermissionPattern.set(JSON.stringify([rule.tool, rule.pattern]), {
      action: rule.allow ? "allow" as const : "deny" as const,
      permission: rule.tool,
      pattern: rule.pattern,
    });
  }
  return [...byPermissionPattern.values()];
}
