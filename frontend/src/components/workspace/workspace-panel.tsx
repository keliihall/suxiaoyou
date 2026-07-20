"use client";

import { WORKSPACE_PANEL_WIDTH, IS_DESKTOP, TITLE_BAR_HEIGHT } from "@/lib/constants";
import { useIsMacOS } from "@/hooks/use-platform";
import { ProgressCard } from "./progress-section";
import { FilesCard } from "./files-section";
import { ContextCard } from "./context-section";
import { GoalCard } from "@/components/goal/goal-card";
import { useChatStore } from "@/stores/chat-store";
import { useWorkspaceStore } from "@/stores/workspace-store";
import { RuntimeControlCard } from "./runtime-control-card";
import { UserOfficeTemplateCard } from "./user-office-template-card";

export function WorkspacePanel() {
  const isMac = useIsMacOS();
  const focusedSessionId = useChatStore((state) => state.focusedSessionId);
  const activeWorkspacePath = useWorkspaceStore((state) => state.activeWorkspacePath);
  const topOffset = IS_DESKTOP && !isMac ? TITLE_BAR_HEIGHT : 0;
  return (
    <aside
      className="fixed inset-y-0 right-0 z-30 flex flex-col overflow-hidden bg-[var(--surface-chat)]"
      style={{
        width: WORKSPACE_PANEL_WIDTH,
        top: topOffset,
      }}
    >
      <div className="flex-1 overflow-y-auto overscroll-contain px-3 py-4 space-y-3 scrollbar-auto">
        <GoalCard sessionId={focusedSessionId} />
        {activeWorkspacePath && (
          <>
            <RuntimeControlCard
              key={`runtime-${focusedSessionId ?? "none"}`}
              sessionId={focusedSessionId}
            />
            <UserOfficeTemplateCard
              key={`office-templates-${focusedSessionId ?? "none"}`}
              sessionId={focusedSessionId}
            />
          </>
        )}
        <ProgressCard />
        <FilesCard />
        <ContextCard />
      </div>
    </aside>
  );
}
