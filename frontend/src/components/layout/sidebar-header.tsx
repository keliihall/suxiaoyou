"use client";

import { IS_DESKTOP } from "@/lib/constants";

/**
 * Empty strip at the top of the sidebar.
 *
 * The strip reserves room for the floating desktop actions (and the macOS
 * traffic lights) while also providing a drag region. Web mode keeps only a
 * compact spacer because WindowTopIcons is desktop-only.
 */
export function SidebarHeader() {
  return (
    <div
      data-tauri-drag-region
      aria-hidden="true"
      style={{ height: IS_DESKTOP ? 48 : 12 }}
    />
  );
}
