export type FileArtifactActionId =
  | "preview"
  | "openDefault"
  | "openOther"
  | "reveal"
  | "copyPath"
  | "saveCopy";

/**
 * Keep system-level file actions off remote and browser clients.
 *
 * A remote client may point at a real path on the host, but launching host
 * applications or exposing that absolute path would be surprising and can
 * leak local filesystem details. Preview and download remain available
 * because they already travel through the authenticated file-content API.
 */
export function getFileArtifactActionIds(
  localDesktop: boolean,
): FileArtifactActionId[] {
  if (!localDesktop) return ["preview", "saveCopy"];
  return ["preview", "openDefault", "openOther", "reveal", "copyPath", "saveCopy"];
}
