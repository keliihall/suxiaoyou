import type { PartData, ToolPart } from "../types/message.ts";

export const VISIBLE_TOOL_PARTS = new Set([
  "artifact",
  "present_file",
  "submit_plan",
]);

export const FILE_CARD_TOOL_PARTS = new Set([
  "present_file",
  "write",
  "edit",
  "image_generate",
  "office",
  "code_execute",
  "bash",
]);

export const GENERATED_FILE_TOOL_PARTS = new Set([
  "write",
  "edit",
  "image_generate",
  "office",
  "code_execute",
  "bash",
]);

const FILE_CARD_EXTENSIONS = new Set([
  ".aac",
  ".avi",
  ".bmp",
  ".csv",
  ".docx",
  ".flac",
  ".gif",
  ".html",
  ".htm",
  ".jpeg",
  ".jpg",
  ".json",
  ".m4a",
  ".md",
  ".mdx",
  ".mkv",
  ".mov",
  ".mp3",
  ".mp4",
  ".ogg",
  ".opus",
  ".pdf",
  ".png",
  ".ppt",
  ".pptx",
  ".svg",
  ".tsv",
  ".txt",
  ".wav",
  ".webm",
  ".webp",
  ".xls",
  ".xlsx",
  ".zip",
]);

const NON_USER_FACING_FILE_HINTS = ["helper", "scratch", "temp", "tmp", "script"];
const NON_USER_FACING_PATH_SEGMENTS = [
  ".git",
  ".suxiaoyou",
  ".venv",
  "__pycache__",
  "node_modules",
];

function fileExtension(filePath: string): string {
  const lastSlash = Math.max(filePath.lastIndexOf("/"), filePath.lastIndexOf("\\"));
  const fileName = filePath.slice(lastSlash + 1);
  const dot = fileName.lastIndexOf(".");
  return dot >= 0 ? fileName.slice(dot).toLowerCase() : "";
}

function isUserFacingGeneratedFile(filePath: string): boolean {
  const lastSlash = Math.max(filePath.lastIndexOf("/"), filePath.lastIndexOf("\\"));
  const fileName = filePath.slice(lastSlash + 1).toLowerCase();
  const pathSegments = filePath.toLowerCase().split(/[/\\]+/);
  if (!FILE_CARD_EXTENSIONS.has(fileExtension(filePath))) return false;
  if (pathSegments.some((segment) => NON_USER_FACING_PATH_SEGMENTS.includes(segment))) {
    return false;
  }
  return !NON_USER_FACING_FILE_HINTS.some((hint) => fileName.includes(hint));
}

function isExplicitArtifactFile(part: ToolPart, filePath: string): boolean {
  const metadata = (part.state.metadata ?? {}) as Record<string, unknown>;
  if (metadata.artifact_delivery === true && metadata.file_path === filePath) return true;
  if (!Array.isArray(metadata.artifact_files)) return false;
  return metadata.artifact_files.some((value) => {
    if (value === filePath) return true;
    if (!value || typeof value !== "object") return false;
    const item = value as Record<string, unknown>;
    return item.path === filePath || item.file_path === filePath;
  });
}

export function collectToolFilePaths(part: ToolPart): string[] {
  const input = part.state.input as Record<string, unknown>;
  const metadata = (part.state.metadata ?? {}) as Record<string, unknown>;

  if (part.tool === "present_file") {
    const filePath = metadata.file_path || input.file_path;
    return typeof filePath === "string" ? [filePath] : [];
  }

  const declared: string[] = [];
  if (
    (
      part.tool === "write" ||
      part.tool === "edit" ||
      part.tool === "image_generate" ||
      part.tool === "office"
    ) &&
    typeof metadata.file_path === "string"
  ) {
    declared.push(metadata.file_path);
  }

  for (const key of ["artifact_files", "written_files"] as const) {
    const values = metadata[key];
    if (!Array.isArray(values)) continue;
    for (const value of values) {
      if (typeof value === "string" && value) {
        declared.push(value);
      } else if (value && typeof value === "object") {
        const item = value as Record<string, unknown>;
        const path = item.path || item.file_path;
        if (typeof path === "string" && path) declared.push(path);
      }
    }
  }

  if (typeof metadata.file_path === "string" && metadata.artifact_delivery === true) {
    declared.push(metadata.file_path);
  }

  return [...new Set(declared)];
}

export function isFileCardToolPart(part: PartData): boolean {
  return (
    part.type === "tool" &&
    (
      FILE_CARD_TOOL_PARTS.has((part as ToolPart).tool) ||
      collectToolFilePaths(part as ToolPart).length > 0
    )
  );
}

export function fileCardsForTool(part: ToolPart, presentedFilePaths: Set<string>) {
  const input = part.state.input as Record<string, unknown>;
  const metadata = (part.state.metadata ?? {}) as Record<string, unknown>;
  const title =
    typeof metadata.title === "string"
      ? metadata.title
      : typeof input.title === "string"
        ? input.title
        : undefined;

  return collectToolFilePaths(part)
    .filter((filePath) =>
      part.tool === "present_file"
        ? !!filePath
        : (
            isExplicitArtifactFile(part, filePath) ||
            isUserFacingGeneratedFile(filePath)
          ) && !presentedFilePaths.has(filePath),
    )
    .map((filePath) => ({ filePath, title: part.tool === "present_file" ? title : undefined }));
}

export function presentedFilePathsForParts(parts: PartData[]): Set<string> {
  const paths = new Set<string>();
  for (const part of parts) {
    if (part.type !== "tool" || part.tool !== "present_file") continue;
    for (const filePath of collectToolFilePaths(part)) paths.add(filePath);
  }
  return paths;
}

/** True when MessageContent will render something outside the activity trace. */
export function hasVisibleMessageOutput(parts: PartData[]): boolean {
  const presentedFilePaths = presentedFilePathsForParts(parts);
  return parts.some((part) => {
    if (
      part.type === "text" ||
      part.type === "file" ||
      part.type === "compaction" ||
      part.type === "subtask"
    ) {
      return true;
    }
    if (part.type !== "tool") return false;
    if (part.tool === "artifact" && part.state.status === "error") return false;
    if (VISIBLE_TOOL_PARTS.has(part.tool)) return true;
    return (
      (GENERATED_FILE_TOOL_PARTS.has(part.tool) || collectToolFilePaths(part).length > 0) &&
      fileCardsForTool(part, presentedFilePaths).length > 0
    );
  });
}
