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

export interface SecurityOverview {
  state: SecurityState;
  warnings?: string[];
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
