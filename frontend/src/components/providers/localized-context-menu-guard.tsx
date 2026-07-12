"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { CheckSquare, Clipboard, ClipboardPaste, Scissors } from "lucide-react";
import { toast } from "sonner";
import { useTranslation } from "react-i18next";
import {
  getLocalizedContextMenuActionIds,
  type LocalizedContextMenuActionId,
} from "@/lib/localized-context-menu";
import { cn } from "@/lib/utils";

const MENU_WIDTH = 168;
const MENU_MARGIN = 8;

interface MenuState {
  x: number;
  y: number;
  target: HTMLElement;
  isEditable: boolean;
  hasSelection: boolean;
}

const ACTION_ICONS: Record<LocalizedContextMenuActionId, typeof Clipboard> = {
  cut: Scissors,
  copy: Clipboard,
  paste: ClipboardPaste,
  selectAll: CheckSquare,
};

const ACTION_LABEL_KEYS: Record<LocalizedContextMenuActionId, string> = {
  cut: "contextCut",
  copy: "contextCopy",
  paste: "contextPaste",
  selectAll: "contextSelectAll",
};

export function LocalizedContextMenuGuard() {
  const { t } = useTranslation("common");
  const [menu, setMenu] = useState<MenuState | null>(null);
  const menuRef = useRef<HTMLDivElement | null>(null);

  const close = useCallback(() => setMenu(null), []);

  useEffect(() => {
    const handleContextMenu = (event: MouseEvent) => {
      if (event.defaultPrevented) return;
      const target = event.target instanceof HTMLElement
        ? event.target
        : event.target instanceof Node
          ? event.target.parentElement
          : null;
      if (
        !target ||
        target.closest("[data-allow-native-context-menu], [data-app-context-menu]")
      ) {
        return;
      }

      event.preventDefault();
      const isEditable = isEditableElement(target);
      const hasSelection = getSelectedText(target).length > 0;
      setMenu({
        x: clamp(event.clientX, MENU_MARGIN, window.innerWidth - MENU_WIDTH - MENU_MARGIN),
        y: clamp(event.clientY, MENU_MARGIN, window.innerHeight - 180),
        target,
        isEditable,
        hasSelection,
      });
    };

    const handlePointerDown = (event: PointerEvent) => {
      if (!menuRef.current?.contains(event.target as Node)) close();
    };
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") close();
    };

    document.addEventListener("contextmenu", handleContextMenu);
    document.addEventListener("pointerdown", handlePointerDown, true);
    document.addEventListener("keydown", handleKeyDown);
    window.addEventListener("blur", close);
    window.addEventListener("scroll", close, true);

    return () => {
      document.removeEventListener("contextmenu", handleContextMenu);
      document.removeEventListener("pointerdown", handlePointerDown, true);
      document.removeEventListener("keydown", handleKeyDown);
      window.removeEventListener("blur", close);
      window.removeEventListener("scroll", close, true);
    };
  }, [close]);

  const actions = useMemo(
    () =>
      menu
        ? getLocalizedContextMenuActionIds({
            isEditable: menu.isEditable,
            hasSelection: menu.hasSelection,
          })
        : [],
    [menu],
  );

  const runAction = useCallback(
    async (action: LocalizedContextMenuActionId) => {
      if (!menu) return;
      try {
        if (action === "copy") {
          const text = getSelectedText(menu.target);
          if (text) await navigator.clipboard.writeText(text);
          toast.success(t("contextCopied"));
        } else if (action === "cut") {
          const text = getSelectedText(menu.target);
          if (text) await navigator.clipboard.writeText(text);
          deleteEditableSelection(menu.target);
        } else if (action === "paste") {
          const text = await navigator.clipboard.readText();
          insertText(menu.target, text);
        } else if (action === "selectAll") {
          selectAll(menu.target, menu.isEditable);
        }
      } catch {
        toast.error(action === "paste" ? t("contextPasteFailed") : t("copyFailed"));
      } finally {
        close();
      }
    },
    [close, menu, t],
  );

  if (!menu) return null;

  return (
    <div
      ref={menuRef}
      data-localized-context-menu
      className="fixed z-[100] min-w-[168px] overflow-hidden rounded-lg border border-[var(--border-default)] bg-[var(--surface-primary)] p-0.5 text-[var(--text-primary)] shadow-[var(--shadow-lg)]"
      style={{ left: menu.x, top: menu.y, width: MENU_WIDTH }}
      role="menu"
      onContextMenu={(event) => event.preventDefault()}
      onMouseDown={(event) => event.preventDefault()}
    >
      {actions.map((action) => {
        const Icon = ACTION_ICONS[action];
        const disabled = (action === "copy" || action === "cut") && !menu.hasSelection;
        return (
          <button
            key={action}
            type="button"
            role="menuitem"
            disabled={disabled}
            onClick={() => void runAction(action)}
            className={cn(
              "flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-[13px] leading-5 outline-none transition-colors",
              "hover:bg-[var(--surface-secondary)] focus-visible:bg-[var(--surface-secondary)]",
              disabled && "cursor-not-allowed opacity-45 hover:bg-transparent",
            )}
          >
            <Icon className="h-3.5 w-3.5 text-[var(--text-tertiary)]" />
            <span>{t(ACTION_LABEL_KEYS[action])}</span>
          </button>
        );
      })}
    </div>
  );
}

function isEditableElement(target: HTMLElement): boolean {
  const editable = target.closest("input, textarea, [contenteditable=''], [contenteditable='true']");
  return Boolean(editable);
}

function getEditableElement(target: HTMLElement): HTMLElement | null {
  return target.closest("input, textarea, [contenteditable=''], [contenteditable='true']");
}

function getSelectedText(target: HTMLElement): string {
  const editable = getEditableElement(target);
  if (isTextControl(editable)) {
    const start = editable.selectionStart ?? 0;
    const end = editable.selectionEnd ?? start;
    return editable.value.slice(start, end);
  }
  return window.getSelection()?.toString() ?? "";
}

function deleteEditableSelection(target: HTMLElement) {
  const editable = getEditableElement(target);
  if (isTextControl(editable)) {
    const start = editable.selectionStart ?? 0;
    const end = editable.selectionEnd ?? start;
    editable.setRangeText("", start, end, "start");
    editable.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "deleteContentBackward" }));
    return;
  }
  if (editable?.isContentEditable) {
    editable.focus();
    document.execCommand("delete");
  }
}

function insertText(target: HTMLElement, text: string) {
  const editable = getEditableElement(target);
  if (!editable) return;
  if (isTextControl(editable)) {
    const start = editable.selectionStart ?? editable.value.length;
    const end = editable.selectionEnd ?? start;
    editable.setRangeText(text, start, end, "end");
    editable.dispatchEvent(new InputEvent("input", { bubbles: true, data: text, inputType: "insertText" }));
    return;
  }
  if (editable.isContentEditable) {
    editable.focus();
    document.execCommand("insertText", false, text);
  }
}

function selectAll(target: HTMLElement, isEditable: boolean) {
  const editable = getEditableElement(target);
  if (isEditable && isTextControl(editable)) {
    editable.focus();
    editable.select();
    return;
  }
  if (isEditable && editable?.isContentEditable) {
    const range = document.createRange();
    range.selectNodeContents(editable);
    const selection = window.getSelection();
    selection?.removeAllRanges();
    selection?.addRange(range);
    return;
  }
  document.execCommand("selectAll");
}

function isTextControl(element: Element | null): element is HTMLInputElement | HTMLTextAreaElement {
  return element instanceof HTMLInputElement || element instanceof HTMLTextAreaElement;
}

function clamp(value: number, min: number, max: number): number {
  return Math.max(min, Math.min(value, max));
}
