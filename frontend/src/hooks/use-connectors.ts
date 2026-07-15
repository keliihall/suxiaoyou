"use client";

import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import type { TFunction } from "i18next";
import { toast } from "sonner";
import { api } from "@/lib/api";
import { API, queryKeys } from "@/lib/constants";
import type { ConnectorsResponse } from "@/types/connectors";

type PluginTranslator = TFunction<"plugins">;

interface ConnectorMutationResult {
  success: boolean;
  error?: string | null;
  error_code?: string | null;
}

const CONNECTOR_ERROR_KEYS: Record<string, string> = {
  connector_system_unavailable: "connectorErrorSystemUnavailable",
  connector_not_found: "connectorErrorNotFound",
  connector_invalid: "connectorErrorInvalid",
  connector_not_custom: "connectorErrorNotCustom",
  connector_local_approval_not_required: "connectorErrorApprovalNotRequired",
  connector_enable_required: "connectorErrorEnableRequired",
  explicit_confirmation_required: "connectorErrorConfirmationRequired",
  local_command_changed: "connectorErrorCommandChanged",
  local_approval_persist_failed: "connectorErrorApprovalPersistence",
  local_approval_connect_failed: "connectorErrorApprovalConnect",
  google_oauth_required: "connectorErrorGoogleOauth",
  invalid_connector_token: "connectorErrorInvalidToken",
  connector_token_unsupported: "connectorErrorTokenUnsupported",
  personal_token_required: "connectorErrorPersonalTokenRequired",
  oauth_discovery_failed: "connectorErrorOauthDiscovery",
  connector_enable_failed: "connectorToggleFailed",
  connector_disable_failed: "connectorToggleFailed",
  connector_auth_callback_failed: "connectorConnectFailed",
  connector_disconnect_failed: "connectorDisconnectFailed",
  connector_reconnect_failed: "connectorReconnectFailed",
};

class LocalizedConnectorError extends Error {}

function localizedFailure(
  t: PluginTranslator,
  errorCode: string | null | undefined,
  fallbackKey: string,
) {
  return t((errorCode && CONNECTOR_ERROR_KEYS[errorCode]) || fallbackKey);
}

function ensureSuccess<T extends ConnectorMutationResult>(
  t: PluginTranslator,
  result: T,
  fallbackKey: string,
): T {
  if (!result.success) {
    throw new LocalizedConnectorError(
      localizedFailure(t, result.error_code, fallbackKey),
    );
  }
  return result;
}

function errorDetail(
  error: unknown,
  t: PluginTranslator,
  fallbackKey: string,
) {
  if (error instanceof LocalizedConnectorError) return error.message;
  if (
    typeof error === "object" &&
    error &&
    "body" in error &&
    typeof (error as { body?: unknown }).body === "object" &&
    (error as { body?: Record<string, unknown> }).body
  ) {
    const body = (error as { body?: Record<string, unknown> }).body;
    const errorCode = body?.error_code;
    if (typeof errorCode === "string") {
      return localizedFailure(t, errorCode, fallbackKey);
    }
  }
  return t(fallbackKey);
}

export function useConnectors() {
  return useQuery({
    queryKey: queryKeys.connectors,
    queryFn: () => api.get<ConnectorsResponse>(API.CONNECTORS.LIST),
    staleTime: 30_000,
    refetchInterval: 60_000,
  });
}

export function useConnectorToggle() {
  const queryClient = useQueryClient();
  const { t } = useTranslation("plugins");
  return useMutation({
    mutationFn: async ({ id, enable }: { id: string; enable: boolean }) => {
      const result = await api.post<ConnectorMutationResult>(
        enable ? API.CONNECTORS.ENABLE(id) : API.CONNECTORS.DISABLE(id),
      );
      return ensureSuccess(t, result, "connectorToggleFailed");
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.connectors });
    },
    onError: (error) => {
      toast.error(errorDetail(error, t, "connectorToggleFailed"));
    },
  });
}

export function useConnectorConnect() {
  const { t } = useTranslation("plugins");
  return useMutation({
    mutationFn: async (id: string) => {
      const result = await api.post<ConnectorMutationResult & {
        auth_url?: string;
        state?: string;
      }>(
        API.CONNECTORS.CONNECT(id),
      );
      return ensureSuccess(t, result, "connectorConnectFailed");
    },
    onError: (error) => {
      toast.error(errorDetail(error, t, "connectorConnectFailed"));
    },
  });
}

export function useConnectorDisconnect() {
  const queryClient = useQueryClient();
  const { t } = useTranslation("plugins");
  return useMutation({
    mutationFn: async (id: string) => {
      const result = await api.post<ConnectorMutationResult>(
        API.CONNECTORS.DISCONNECT(id),
      );
      return ensureSuccess(t, result, "connectorDisconnectFailed");
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.connectors });
    },
    onError: (error) => {
      toast.error(errorDetail(error, t, "connectorDisconnectFailed"));
    },
  });
}

export function useConnectorReconnect() {
  const queryClient = useQueryClient();
  const { t } = useTranslation("plugins");
  return useMutation({
    mutationFn: async (id: string) => {
      const result = await api.post<ConnectorMutationResult>(
        API.CONNECTORS.RECONNECT(id),
      );
      return ensureSuccess(t, result, "connectorReconnectFailed");
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.connectors });
    },
    onError: (error) => {
      toast.error(errorDetail(error, t, "connectorReconnectFailed"));
    },
  });
}

export function useApproveLocalStartup() {
  const queryClient = useQueryClient();
  const { t } = useTranslation("plugins");
  return useMutation({
    mutationFn: async (
      { id, fingerprint }: { id: string; fingerprint: string },
    ) => {
      const result = await api.post<{
        success: boolean;
        error?: string | null;
        error_code?: string | null;
      }>(API.CONNECTORS.APPROVE_LOCAL_STARTUP(id), {
        fingerprint,
        confirmed: true,
      });
      return ensureSuccess(t, result, "localApprovalFailed");
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.connectors });
    },
    onError: (error) => {
      toast.error(errorDetail(error, t, "localApprovalFailed"));
    },
  });
}

export function useSetConnectorToken() {
  const queryClient = useQueryClient();
  const { t } = useTranslation("plugins");
  return useMutation({
    mutationFn: async ({ id, token }: { id: string; token: string }) => {
      const result = await api.post<ConnectorMutationResult>(
        API.CONNECTORS.SET_TOKEN(id),
        { token },
      );
      return ensureSuccess(t, result, "connectorTokenSaveFailed");
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.connectors });
    },
    onError: (error) => {
      toast.error(errorDetail(error, t, "connectorTokenSaveFailed"));
    },
  });
}

export function useAddCustomConnector() {
  const queryClient = useQueryClient();
  const { t } = useTranslation("plugins");
  return useMutation({
    mutationFn: async (body: {
      id: string;
      name: string;
      url: string;
      description?: string;
      category?: string;
    }) => {
      const result = await api.post<ConnectorMutationResult>(
        API.CONNECTORS.ADD,
        body,
      );
      return ensureSuccess(t, result, "connectorAddFailed");
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.connectors });
    },
    onError: (error) => {
      toast.error(errorDetail(error, t, "connectorAddFailed"));
    },
  });
}

export function useRemoveConnector() {
  const queryClient = useQueryClient();
  const { t } = useTranslation("plugins");
  return useMutation({
    mutationFn: async (id: string) => {
      const result = await api.delete<ConnectorMutationResult>(
        API.CONNECTORS.REMOVE(id),
      );
      return ensureSuccess(t, result, "connectorRemoveFailed");
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: queryKeys.connectors });
    },
    onError: (error) => {
      toast.error(errorDetail(error, t, "connectorRemoveFailed"));
    },
  });
}
