/** Pure navigation helpers shared by the outline hook, UI, and unit tests. */

export function conversationHistoryWindowOffsets(
  messageOffset: number,
  totalMessages: number,
  pageSize: number,
): number[] {
  if (totalMessages <= 0 || pageSize <= 0) return [];
  const safeOffset = Math.min(
    Math.max(0, messageOffset),
    Math.max(0, totalMessages - 1),
  );
  const targetPageOffset = Math.floor(safeOffset / pageSize) * pageSize;
  return [...new Set([
    targetPageOffset - pageSize,
    targetPageOffset,
    targetPageOffset + pageSize,
  ].filter((offset) => offset >= 0 && offset < totalMessages))]
    .sort((a, b) => a - b);
}

export function conversationOutlineKeyTarget(
  key: string,
  currentIndex: number,
  turnCount: number,
): number | null {
  if (turnCount <= 0 || currentIndex < 0 || currentIndex >= turnCount) {
    return null;
  }
  let target = currentIndex;
  if (key === "ArrowUp") target = Math.max(0, currentIndex - 1);
  else if (key === "ArrowDown") target = Math.min(turnCount - 1, currentIndex + 1);
  else if (key === "Home") target = 0;
  else if (key === "End") target = turnCount - 1;
  else return null;
  return target === currentIndex ? null : target;
}
