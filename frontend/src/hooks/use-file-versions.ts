"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { API, queryKeys } from "@/lib/constants";
import type {
  FileVersionListResponse,
  FileVersionRestoreResponse,
} from "@/types/file-version";

export function useFileVersions(
  sessionId: string | null | undefined,
  filePath: string,
  enabled: boolean,
) {
  return useQuery({
    queryKey: queryKeys.fileVersions.list(sessionId ?? "", filePath),
    queryFn: ({ signal }) =>
      api.get<FileVersionListResponse>(
        API.FILE_VERSIONS.LIST(sessionId!, filePath),
        { signal },
      ),
    enabled: enabled && Boolean(sessionId) && Boolean(filePath),
    staleTime: 10_000,
  });
}

export function useRestoreFileVersion(
  sessionId: string | null | undefined,
  filePath: string,
) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (versionId: string) =>
      api.post<FileVersionRestoreResponse>(API.FILE_VERSIONS.RESTORE(versionId), {
        session_id: sessionId,
      }),
    onSuccess: async () => {
      await queryClient.invalidateQueries({
        queryKey: queryKeys.fileVersions.list(sessionId ?? "", filePath),
      });
    },
  });
}
