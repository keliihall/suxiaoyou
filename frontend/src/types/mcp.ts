/** MCP (Model Context Protocol) server types */

export interface McpServerStatus {
  status: "connected" | "disconnected" | "failed" | "disabled" | "needs_auth" | "needs_approval";
  error: string | null;
  type: "local" | "remote";
  tools: number;
}

export interface McpStatusResponse {
  servers: Record<string, McpServerStatus>;
}
