/** Maximum number of Unicode code points accepted in a goal objective. */
export const GOAL_OBJECTIVE_MAX_CHARACTERS = 4_000;

export type GoalCommandAction =
  | "view"
  | "create"
  | "edit"
  | "pause"
  | "resume"
  | "clear";

export type GoalCommandName = "goal" | "目标";

export type GoalCommandErrorCode =
  | "objective_required"
  | "objective_too_long"
  | "unexpected_argument";

interface GoalCommandBase {
  /** The normalized root command, useful for analytics without retaining input. */
  command: GoalCommandName;
  action: GoalCommandAction;
}

export type ParsedGoalCommand = GoalCommandBase & {
  ok: true;
  objective?: string;
};

export type InvalidGoalCommand = GoalCommandBase & {
  ok: false;
  error: GoalCommandErrorCode;
  /** Present for length errors so the UI can show a precise, localized message. */
  objectiveCharacters?: number;
  maxObjectiveCharacters?: number;
};

/**
 * `null` means the input is an ordinary chat message, not a Goal command.
 * Recognized commands always return either a valid command or a validation
 * error so callers never need to send an invalid command to the model.
 */
export type GoalCommandParseResult = ParsedGoalCommand | InvalidGoalCommand | null;

const ACTION_ALIASES: Readonly<Record<string, GoalCommandAction>> = {
  // View/status
  view: "view",
  show: "view",
  status: "view",
  "查看": "view",
  "状态": "view",

  // Create/set
  create: "create",
  new: "create",
  set: "create",
  "新建": "create",
  "创建": "create",
  "设定": "create",

  // Edit/update
  edit: "edit",
  update: "edit",
  change: "edit",
  "编辑": "edit",
  "修改": "edit",

  // Pause
  pause: "pause",
  "暂停": "pause",

  // Resume
  resume: "resume",
  continue: "resume",
  "继续": "resume",
  "恢复": "resume",

  // Clear/delete
  clear: "clear",
  delete: "clear",
  "清除": "clear",
  "删除": "clear",
};

const OBJECTIVE_ACTIONS: ReadonlySet<GoalCommandAction> = new Set([
  "create",
  "edit",
]);

/** Count Unicode code points rather than UTF-16 code units (emoji count as one). */
export function countGoalObjectiveCharacters(value: string): number {
  return [...value].length;
}

function validateObjective(
  command: GoalCommandName,
  action: "create" | "edit",
  objective: string,
): ParsedGoalCommand | InvalidGoalCommand {
  if (!objective) {
    return { ok: false, command, action, error: "objective_required" };
  }

  const objectiveCharacters = countGoalObjectiveCharacters(objective);
  if (objectiveCharacters > GOAL_OBJECTIVE_MAX_CHARACTERS) {
    return {
      ok: false,
      command,
      action,
      error: "objective_too_long",
      objectiveCharacters,
      maxObjectiveCharacters: GOAL_OBJECTIVE_MAX_CHARACTERS,
    };
  }

  return { ok: true, command, action, objective };
}

/**
 * Parse a Goal slash command without depending on React, browser state, or i18n.
 *
 * Integration contract:
 * - Call this before ordinary prompt/queue submission on every composer.
 * - `null`: continue with the existing chat send path unchanged.
 * - `{ ok: false }`: keep the draft/attachments and show a localized error.
 * - `{ ok: true }`: dispatch the Goal action and never send the command text to
 *   the model as an ordinary user message.
 * - Enforce composer-only policy (for example, disallowing attachments) before
 *   dispatch, and clear the draft/attachments only after the action is accepted.
 */
export function parseGoalCommand(input: string): GoalCommandParseResult {
  // Deliberately do not trimStart(): a command must begin at the first input
  // character. The lookahead prevents near-matches such as /goals or /目标化.
  const rootMatch = /^\/(goal|目标)(?=$|\s)/iu.exec(input);
  if (!rootMatch) return null;

  const rawCommand = rootMatch[1];
  const command: GoalCommandName = rawCommand.toLocaleLowerCase("en-US") === "goal"
    ? "goal"
    : "目标";
  const remainder = input.slice(rootMatch[0].length).trim();

  if (!remainder) return { ok: true, command, action: "view" };

  const tokenMatch = /^(\S+)(?:\s+([\s\S]*))?$/u.exec(remainder);
  if (!tokenMatch) return null; // Defensive; a non-empty remainder always matches.

  const firstToken = tokenMatch[1].toLocaleLowerCase("en-US");
  const explicitAction = ACTION_ALIASES[firstToken];

  // An unrecognized first token is the beginning of the implicit create
  // objective: `/goal ship the report`.
  if (!explicitAction) {
    return validateObjective(command, "create", remainder);
  }

  const argument = (tokenMatch[2] ?? "").trim();
  if (OBJECTIVE_ACTIONS.has(explicitAction)) {
    return validateObjective(
      command,
      explicitAction as "create" | "edit",
      argument,
    );
  }

  if (argument) {
    return {
      ok: false,
      command,
      action: explicitAction,
      error: "unexpected_argument",
    };
  }

  return { ok: true, command, action: explicitAction };
}
