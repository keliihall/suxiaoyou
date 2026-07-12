/** Chat request/response schemas — mirrors backend app/schemas/chat.py */

/** Metadata returned by the upload endpoint, sent with the prompt. */
export interface FileAttachment {
  file_id: string;
  name: string;
  path: string;
  size: number;
  mime_type: string;
  source?: "referenced" | "uploaded" | "managed";
  content_hash?: string;
}

export interface PromptRequest {
  client_request_id?: string;
  session_id?: string | null;
  text: string;
  model?: string | null;
  provider_id?: string | null;
  agent?: string;
  attachments?: FileAttachment[];
  permission_presets?: Record<string, boolean> | null;
  permission_rules?: Array<{ action: "allow" | "deny"; permission: string; pattern?: string }> | null;
  reasoning?: boolean | null;
  workspace?: string | null;
}

export interface PromptResponse {
  stream_id: string;
  session_id: string;
}

export type SessionInputMode = "queue" | "steer";
export type SessionInputStatus = "queued" | "applying" | "blocked" | "consumed" | "failed" | "cancelled";

/** A follow-up submitted while the session already has an active generation. */
export interface SessionInputRequest {
  session_id: string;
  client_request_id: string;
  mode: SessionInputMode;
  text: string;
  attachments?: FileAttachment[];
  model?: string | null;
  provider_id?: string | null;
  agent?: string;
  workspace?: string | null;
  reasoning?: boolean | null;
  permission_presets?: Record<string, boolean> | null;
  permission_rules?: Array<{ action: "allow" | "deny"; permission: string; pattern?: string }> | null;
}

export interface SessionInputResponse {
  id: string;
  session_id: string;
  client_request_id: string;
  mode: SessionInputMode;
  status: SessionInputStatus;
  position: number;
  text: string;
  attachments: FileAttachment[];
  target_stream_id?: string | null;
  error_message?: string | null;
}

export interface SessionInputUpdateRequest {
  mode?: SessionInputMode;
  move?: "up" | "down";
  position?: number;
}

export type TaskBatchMode = "sequential" | "parallel";

export interface TaskBatchTask {
  title: string;
  prompt: string;
  agent: string;
  model?: string | null;
  provider_id?: string | null;
}

export interface TaskBatchRequest {
  session_id?: string | null;
  mode: TaskBatchMode;
  tasks: TaskBatchTask[];
  workspace?: string | null;
}

export interface EditAndResendRequest {
  session_id: string;
  message_id: string;
  text: string;
  model?: string | null;
  provider_id?: string | null;
  agent?: string;
  attachments?: FileAttachment[];
  permission_presets?: Record<string, boolean> | null;
  permission_rules?: Array<{ action: "allow" | "deny"; permission: string; pattern?: string }> | null;
  reasoning?: boolean | null;
  workspace?: string | null;
}

export type EditAndResendResult =
  | { status: "reconciled" }
  | { status: "committed_unreconciled" }
  | { status: "failed" };

export interface AbortRequest {
  stream_id: string;
}

export interface RespondRequest {
  stream_id: string;
  call_id: string;
  response: unknown;
}

export interface RespondResult {
  status: "accepted" | "already_resolved";
  call_id: string;
  tool_call_id?: string | null;
  tool?: string | null;
  prompt_type: "permission" | "question" | "plan" | "unknown";
  decision: string;
  source: string;
  idempotent: boolean;
}
