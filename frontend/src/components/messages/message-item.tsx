"use client";

import { memo } from "react";
import { UserMessage } from "./user-message";
import { AssistantMessage } from "./assistant-message";
import type { EditAndResendResult, FileAttachment } from "@/types/chat";
import type { MessageResponse } from "@/types/message";

interface MessageItemProps {
  message: MessageResponse;
  onRegenerate?: () => void;
  onEditAndResend?: (messageId: string, newText: string, attachments?: FileAttachment[]) => Promise<EditAndResendResult>;
  isGenerating?: boolean;
  /** Whether this message just arrived (animate) or was loaded from history (skip animation). */
  isNew?: boolean;
  /** Workspace directory for @mention file search in edit mode. */
  directory?: string | null;
  /** Session ID for file ingestion in edit mode. */
  sessionId?: string;
  /** Registers stable message anchors for conversation-outline navigation. */
  onElementChange?: (messageId: string, element: HTMLDivElement | null) => void;
}

export const MessageItem = memo(function MessageItem({ message, onRegenerate, onEditAndResend, isGenerating, isNew = true, directory, sessionId, onElementChange }: MessageItemProps) {
  const role = (message.data as { role: string }).role;

  return (
    <div
      ref={(element) => onElementChange?.(message.id, element)}
      data-message-id={role === "user" ? message.id : undefined}
      className="scroll-mt-3 px-4 py-3"
    >
      <div className="mx-auto max-w-3xl xl:max-w-4xl">
        {role === "user" ? (
          <UserMessage
            message={message}
            isNew={isNew}
            onEditAndResend={onEditAndResend}
            isGenerating={isGenerating}
            directory={directory}
            sessionId={sessionId}
          />
        ) : (
          <AssistantMessage message={message} onRegenerate={onRegenerate} isNew={isNew} />
        )}
      </div>
    </div>
  );
});
