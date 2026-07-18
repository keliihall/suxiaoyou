import { api, type ApiRequestInit } from "@/lib/api";
import type {
  SecurityAuditResponse,
  SecurityHookRevocationResponse,
  SecurityOverview,
  SecurityProjectHooksResponse,
} from "@/types/security";

export const SECURITY_API = {
  overview: "/api/security/overview",
  audit: (limit: number) => `/api/security/audit?limit=${limit}`,
  tool: (id: string) => `/api/security/tools/${encodeURIComponent(id)}`,
  hooks: (sessionId: string) =>
    `/api/security/hooks?session_id=${encodeURIComponent(sessionId)}`,
  revokeHook: "/api/security/hooks/revoke",
  emergencyStop: "/api/security/emergency-stop",
} as const;

export function getSecurityOverview(options?: ApiRequestInit) {
  return api.get<SecurityOverview>(SECURITY_API.overview, options);
}

export function getSecurityAudit(limit = 100, options?: ApiRequestInit) {
  return api.get<SecurityAuditResponse>(SECURITY_API.audit(limit), options);
}

export function setSecurityToolEnabled(id: string, enabled: boolean) {
  return api.put<SecurityOverview>(SECURITY_API.tool(id), { enabled });
}

export function getSecurityProjectHooks(sessionId: string, options?: ApiRequestInit) {
  return api.get<SecurityProjectHooksResponse>(SECURITY_API.hooks(sessionId), options);
}

export function revokeSecurityProjectHook(sessionId: string, hookId: string) {
  return api.post<SecurityHookRevocationResponse>(SECURITY_API.revokeHook, {
    session_id: sessionId,
    hook_id: hookId,
  });
}

export function setEmergencyStop(active: boolean) {
  return api.post<SecurityOverview>(SECURITY_API.emergencyStop, { active });
}
