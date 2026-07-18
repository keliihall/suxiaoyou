"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  getSecurityProjectHooks,
  getSecurityAudit,
  getSecurityOverview,
  revokeSecurityProjectHook,
  setEmergencyStop,
  setSecurityToolEnabled,
} from "@/lib/security-api";
import type { SecurityOverview } from "@/types/security";

export const securityQueryKeys = {
  overview: ["security", "overview"] as const,
  audit: ["security", "audit"] as const,
  hooks: (sessionId: string) => ["security", "hooks", sessionId] as const,
};

function useRefreshSecurityCaches() {
  const queryClient = useQueryClient();
  return (overview: SecurityOverview) => {
    queryClient.setQueryData(securityQueryKeys.overview, overview);
    void queryClient.invalidateQueries({ queryKey: securityQueryKeys.audit });
  };
}

export function useSecurityOverview() {
  return useQuery({
    queryKey: securityQueryKeys.overview,
    queryFn: ({ signal }) => getSecurityOverview({ signal }),
    staleTime: 15_000,
    refetchInterval: 30_000,
  });
}

export function useSecurityAudit(limit = 100) {
  return useQuery({
    queryKey: [...securityQueryKeys.audit, limit],
    queryFn: ({ signal }) => getSecurityAudit(limit, { signal }),
    staleTime: 15_000,
    refetchInterval: 30_000,
  });
}

export function useSecurityProjectHooks(sessionId: string | null, enabled: boolean) {
  return useQuery({
    queryKey: securityQueryKeys.hooks(sessionId ?? "none"),
    queryFn: ({ signal }) => getSecurityProjectHooks(sessionId!, { signal }),
    enabled: enabled && Boolean(sessionId),
    staleTime: 10_000,
    refetchInterval: 30_000,
  });
}

export function useSecurityProjectHookRevocation(sessionId: string | null) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (hookId: string) => {
      if (!sessionId) throw new Error("security_hook_session_unavailable");
      return revokeSecurityProjectHook(sessionId, hookId);
    },
    onSuccess: () => {
      if (sessionId) {
        void queryClient.invalidateQueries({ queryKey: securityQueryKeys.hooks(sessionId) });
      }
      void queryClient.invalidateQueries({ queryKey: securityQueryKeys.audit });
    },
  });
}

export function useSecurityToolToggle() {
  const refreshCaches = useRefreshSecurityCaches();
  return useMutation({
    mutationFn: ({ id, enabled }: { id: string; enabled: boolean }) =>
      setSecurityToolEnabled(id, enabled),
    onSuccess: refreshCaches,
  });
}

export function useEmergencyStopToggle() {
  const refreshCaches = useRefreshSecurityCaches();
  return useMutation({
    mutationFn: (active: boolean) => setEmergencyStop(active),
    onSuccess: refreshCaches,
  });
}
