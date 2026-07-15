/** Connector (individual MCP server connection) types */

export interface LocalStartupApproval {
  required: boolean;
  approved: boolean;
  fingerprint: string | null;
  command: string[];
  cwd: string | null;
  environment_keys: string[];
  error: string | null;
}

export interface ConnectorInfo {
  id: string;
  name: string;
  url: string;
  type: "remote" | "local";
  description: string;
  category: string;
  enabled: boolean;
  connected: boolean;
  status: "connected" | "disconnected" | "needs_auth" | "needs_approval" | "failed" | "disabled";
  error: string | null;
  tools_count: number;
  source: "builtin" | "custom";
  referenced_by: string[];
  auth_mode: "oauth_bearer" | "raw_authorization";
  credential_url: string;
  credential_configured: boolean;
  local_approval: LocalStartupApproval | null;
}

export interface ConnectorsResponse {
  connectors: Record<string, ConnectorInfo>;
}
