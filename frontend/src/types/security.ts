export interface SecurityState {
  emergency_stop: boolean;
  disabled_tools: string[];
  updated_at: string | null;
  degraded_reason: string | null;
}

export interface SecurityTool {
  id: string;
  description: string;
  source_kind: string;
  source_id: string;
  capabilities: string[];
  enabled: boolean;
  requires_approval: boolean;
  toggleable: boolean;
}

export interface SecurityConnector {
  id: string;
  name: string;
  enabled: boolean;
  connected: boolean;
  status: string;
  credential_configured: boolean;
  capabilities: string[];
}

export interface SecurityProvider {
  id: string;
  name: string;
  configured: boolean;
  enabled: boolean;
  capabilities: string[];
}

export interface SecurityProjectHook {
  hook_id: string;
  event: string;
  source: "project";
  failure_policy: string;
  timeout_seconds: number;
  fingerprint: string;
  approval_state: "approved" | "required" | "unavailable";
}

export interface SecurityProjectHooksResponse {
  session_id: string;
  trust_store_available: boolean;
  hooks: SecurityProjectHook[];
}

export interface SecurityHookRevocationResponse {
  session_id: string;
  hook_id: string;
  revoked: boolean;
}

export interface SecurityOverview {
  state: SecurityState;
  warnings?: string[];
  source_profiles?: Array<{
    source: string;
    allowed_capabilities: string[];
    deny_unknown: boolean;
  }>;
  tools: SecurityTool[];
  connectors: SecurityConnector[];
  providers: SecurityProvider[];
  automations: {
    enabled_count: number;
    runtime_running: boolean;
  };
  /** Server-authoritative Goal defaults/ceilings. Null means no token limit. */
  goal_limits?: {
    default_token_budget: number | null;
    max_token_budget: number | null;
  };
  release_gates: {
    remote_access: boolean;
    messaging_channels: boolean;
    /** Persistent goal control plane. Missing means fail closed on older backends. */
    goals?: boolean;
    /** Autonomous continuation is released independently from goal CRUD. */
    autonomous_goals?: boolean;
    /** v1.1 gates remain optional so mixed-version desktop/backend pairs fail closed. */
    v11_checkpoints?: boolean;
    v11_rewind?: boolean;
    v11_hooks?: boolean;
    v11_acp?: boolean;
    v11_worktrees?: boolean;
    v11_validation_agent?: boolean;
    v11_office_v2?: boolean;
    v11_user_office_templates_beta?: boolean;
  };
  /** Dependency-composed source gates plus redacted machine readiness. */
  v11_readiness?: Partial<
    Record<
      | "checkpoints"
      | "rewind"
      | "hooks"
      | "acp"
      | "worktrees"
      | "validator"
      | "office_preview"
      | "office_authoring"
      | "user_office_templates",
      {
        code_gate: boolean;
        released: boolean;
        dependencies: string[];
        missing_dependencies: string[];
        runtime_ready: boolean;
        missing_runtime: string[];
        renderer_quality?: "authoritative" | "approximate" | null;
      }
    >
  >;
  /** Redacted safety contracts for local v1.1 runtime-control surfaces. */
  v11_runtime_capabilities?: {
    checkpoint_rewind: {
      released: boolean;
      local_session_only: boolean;
      server_owned_workspace_identity_required: boolean;
      pre_action_audit_required: boolean;
      external_side_effects_reverted: boolean;
      raw_runtime_payloads_exposed: boolean;
    };
    managed_worktrees: {
      released: boolean;
      local_session_only: boolean;
      repository_derived_from_database: boolean;
      force_remove_supported: boolean;
      pre_action_audit_required: boolean;
      raw_runtime_payloads_exposed: boolean;
    };
  };
}

export interface SecurityAuditEvent {
  id: string;
  source_kind: string;
  source_id: string;
  capability: string;
  action: string;
  decision: string;
  outcome: string;
  session_id: string | null;
  call_id: string | null;
  details: Record<string, unknown> | null;
  time_created: string;
}

export interface SecurityAuditResponse {
  events: SecurityAuditEvent[];
}
