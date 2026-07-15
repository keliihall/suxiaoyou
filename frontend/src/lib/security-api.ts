import { api, type ApiRequestInit } from "@/lib/api";
import type {
  SecurityAuditResponse,
  SecurityOverview,
} from "@/types/security";

export const SECURITY_API = {
  overview: "/api/security/overview",
  audit: (limit: number) => `/api/security/audit?limit=${limit}`,
  tool: (id: string) => `/api/security/tools/${encodeURIComponent(id)}`,
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

export function setEmergencyStop(active: boolean) {
  return api.post<SecurityOverview>(SECURITY_API.emergencyStop, { active });
}
