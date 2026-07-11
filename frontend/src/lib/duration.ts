/**
 * Format an elapsed duration for user-facing status text.
 *
 * Durations are intentionally rendered with whole seconds: live task timers
 * already tick once per second, and keeping the same shape for completed
 * activity avoids switching between decimal and unit-based labels.
 */
export function formatElapsedDuration(
  totalSeconds: number,
  language = "en",
): string {
  const normalizedSeconds = Number.isFinite(totalSeconds)
    ? Math.max(0, Math.floor(totalSeconds))
    : 0;
  const hours = Math.floor(normalizedSeconds / 3600);
  const minutes = Math.floor((normalizedSeconds % 3600) / 60);
  const seconds = normalizedSeconds % 60;
  const isChinese = language.toLowerCase().startsWith("zh");

  if (isChinese) {
    if (hours > 0) {
      return `${hours}小时${padTwo(minutes)}分钟${padTwo(seconds)}秒`;
    }
    if (minutes > 0) {
      return `${minutes}分钟${padTwo(seconds)}秒`;
    }
    return `${seconds}秒`;
  }

  if (hours > 0) {
    return `${hours}h ${padTwo(minutes)}m ${padTwo(seconds)}s`;
  }
  if (minutes > 0) {
    return `${minutes}m ${padTwo(seconds)}s`;
  }
  return `${seconds}s`;
}

/** Format tool runtimes while preserving useful sub-second precision. */
export function formatElapsedMilliseconds(
  totalMilliseconds: number,
  language = "en",
): string {
  const normalizedMilliseconds = Number.isFinite(totalMilliseconds)
    ? Math.max(0, Math.floor(totalMilliseconds))
    : 0;

  if (normalizedMilliseconds < 1000) {
    return language.toLowerCase().startsWith("zh")
      ? `${normalizedMilliseconds}毫秒`
      : `${normalizedMilliseconds}ms`;
  }

  return formatElapsedDuration(normalizedMilliseconds / 1000, language);
}

function padTwo(value: number): string {
  return String(value).padStart(2, "0");
}
