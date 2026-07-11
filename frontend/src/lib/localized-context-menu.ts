export type LocalizedContextMenuActionId = "cut" | "copy" | "paste" | "selectAll";

interface ContextMenuActionOptions {
  isEditable: boolean;
  hasSelection: boolean;
}

export function getLocalizedContextMenuActionIds({
  isEditable,
  hasSelection,
}: ContextMenuActionOptions): LocalizedContextMenuActionId[] {
  if (isEditable) return ["cut", "copy", "paste", "selectAll"];
  return hasSelection ? ["copy", "selectAll"] : ["selectAll"];
}
