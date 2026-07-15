export interface FileVersion {
  id: string;
  relative_path: string;
  sha256: string;
  size: number;
  created_at: string;
  created_at_ns: number;
  operation: string;
  session_id: string | null;
  message_id: string | null;
  call_id: string | null;
  original_mode: number | null;
}

export interface FileVersionListResponse {
  workspace: string;
  versions: FileVersion[];
}

export interface FileVersionRestoreResponse {
  file_path: string;
  restored_version: FileVersion;
  recovery_version: FileVersion | null;
}
