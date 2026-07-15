/** Session goal schemas — mirrors backend app/schemas/goal.py. */

export type GoalStatus =
  | "active"
  | "paused"
  | "blocked"
  | "usage_limited"
  | "budget_limited"
  | "complete";

export type GoalRunState =
  | "idle"
  | "reserved"
  | "running"
  | "pausing"
  | "waiting_user"
  | "interrupted";

export interface SessionGoal {
  id: string;
  session_id: string;
  objective: string;
  definition_of_done: string | null;
  status: GoalStatus;
  run_state: GoalRunState;
  revision: number;

  token_budget: number | null;
  tokens_used: number;
  cost_budget_microusd: number | null;
  cost_used_microusd: number;
  time_budget_seconds: number | null;
  time_used_seconds: number;
  max_continuations: number | null;
  continuation_count: number;
  no_progress_count: number;
  blocker_streak: number;
  consecutive_error_count: number;

  blocker_code: string | null;
  blocker_message: string | null;
  needs_review: boolean;
  next_retry_at: string | null;
  completion_summary: string | null;
  completion_evidence: unknown[] | Record<string, unknown> | null;

  model_id: string | null;
  provider_id: string | null;
  agent: string;
  reasoning: boolean | null;
  language: string;
  last_run_id: string | null;
  last_stream_id: string | null;
  time_started: string | null;
  time_completed: string | null;
  time_created: string;
  time_updated: string;
}

export interface GoalTokenUsage {
  /** Prompt tokens excluding cache hits. */
  input: number;
  output: number;
  reasoning: number;
  /** Prompt tokens read from the Provider cache. */
  cache_read: number;
  /** Legacy/recovered usage whose original component payload is unavailable. */
  unattributed: number;
  /** input + output + reasoning + cache_read + unattributed. */
  total_tokens: number;
  source_count: number;
}

export interface GoalUpdateRequest {
  expected_revision: number;
  client_request_id: string;
  objective?: string;
  definition_of_done?: string | null;
  token_budget?: number | null;
  cost_budget_microusd?: number | null;
  time_budget_seconds?: number | null;
  max_continuations?: number | null;
  model_id?: string | null;
  provider_id?: string | null;
  agent?: string;
  reasoning?: boolean | null;
  language?: "zh" | "en";
}

export interface GoalCreateRequest {
  client_request_id: string;
  objective: string;
  definition_of_done?: string | null;
  token_budget?: number | null;
  cost_budget_microusd?: number | null;
  time_budget_seconds?: number | null;
  max_continuations?: number | null;
  model_id?: string | null;
  provider_id?: string | null;
  agent?: string;
  reasoning?: boolean | null;
  language?: "zh" | "en";
}

export interface GoalControlRequest {
  expected_revision: number;
  client_request_id: string;
}

export interface GoalRun {
  id: string;
  goal_id: string;
  ordinal: number;
  goal_revision: number;
  idempotency_key: string;
  stream_id: string | null;
  trigger: "initial" | "auto" | "resume" | "user_input";
  status: "reserved" | "running" | "waiting_user" | "completed" | "blocked" | "interrupted" | "failed";
  tokens_used: number;
  cost_used_microusd: number;
  active_seconds: number;
  progress_summary: string | null;
  stop_reason: string | null;
  error_code: string | null;
  lease_owner: string | null;
  lease_expires_at: string | null;
  side_effects_started: boolean;
  time_started: string | null;
  time_finished: string | null;
  time_created: string;
  time_updated: string;
}

export interface GoalStartRequest {
  client_request_id: string;
  session_id?: string | null;
  objective: string;
  definition_of_done?: string | null;
  token_budget?: number | null;
  cost_budget_microusd?: number | null;
  time_budget_seconds?: number | null;
  max_continuations?: number | null;
  model?: string | null;
  provider_id?: string | null;
  agent?: string;
  reasoning?: boolean | null;
  workspace?: string | null;
  attachments?: import("./chat").FileAttachment[];
  permission_presets?: Record<string, boolean> | null;
  permission_rules?: Array<{
    action: "allow" | "deny";
    permission: string;
    pattern?: string;
  }> | null;
}

export interface GoalStartResponse {
  stream_id: string;
  session_id: string;
  goal: SessionGoal;
  run: GoalRun;
}
