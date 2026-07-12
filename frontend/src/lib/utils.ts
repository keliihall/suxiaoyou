import { type ClassValue, clsx } from "clsx";
import { twMerge } from "tailwind-merge";
import type { PartData, TextPart } from "@/types/message";

export const APP_TIME_ZONE = "Asia/Shanghai";

const BACKEND_NAIVE_DATETIME_RE = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?$/;
const SHANGHAI_OFFSET_MS = 8 * 60 * 60 * 1000;

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

export function parseBackendDate(dateLike: string | Date): Date {
  if (dateLike instanceof Date) return dateLike;
  const trimmed = dateLike.trim();
  if (BACKEND_NAIVE_DATETIME_RE.test(trimmed)) {
    return new Date(trimmed + "Z");
  }
  return new Date(trimmed);
}

function resolveDisplayLocale(locale: string): "zh-CN" | "en-US" {
  return locale.toLowerCase().startsWith("zh") ? "zh-CN" : "en-US";
}

/**
 * Format the compact timestamp shown beside a task title.
 *
 * Relative wording is intentionally limited to today, yesterday, and the day
 * before yesterday. Older relative labels become hard to map back to a date,
 * so older tasks use a calendar date instead.
 */
export function formatRelativeTime(
  date: string | Date,
  nowDate: Date = new Date(),
  locale = "zh-CN",
): string {
  const d = parseBackendDate(date);
  if (Number.isNaN(d.getTime()) || Number.isNaN(nowDate.getTime())) return "";

  const displayLocale = resolveDisplayLocale(locale);
  const isChinese = displayLocale === "zh-CN";
  const daysAgo = Math.max(
    0,
    dayNumberInAppTimeZone(nowDate) - dayNumberInAppTimeZone(d),
  );
  const diff = Math.max(0, nowDate.getTime() - d.getTime());
  const seconds = Math.floor(diff / 1000);
  const minutes = Math.floor(seconds / 60);
  const hours = Math.floor(minutes / 60);

  if (daysAgo === 0) {
    if (seconds < 60) return isChinese ? "刚刚" : "just now";
    if (minutes < 60) return isChinese ? `${minutes}分钟前` : `${minutes}m ago`;
    return isChinese ? `${hours}小时前` : `${hours}h ago`;
  }
  if (daysAgo === 1) return isChinese ? "昨天" : "yesterday";
  if (daysAgo === 2) return isChinese ? "前天" : "2d ago";

  return d.toLocaleDateString(displayLocale, {
    timeZone: APP_TIME_ZONE,
    month: "short",
    day: "numeric",
    year: yearInAppTimeZone(d) !== yearInAppTimeZone(nowDate) ? "numeric" : undefined,
  });
}

/** Full localized timestamp used by the task-row tooltip and screen readers. */
export function formatFullDateTime(date: string | Date, locale = "zh-CN"): string {
  const d = parseBackendDate(date);
  if (Number.isNaN(d.getTime())) return "";
  return new Intl.DateTimeFormat(resolveDisplayLocale(locale), {
    timeZone: APP_TIME_ZONE,
    year: "numeric",
    month: "long",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hourCycle: "h23",
  }).format(d);
}

function yearInAppTimeZone(date: Date): string {
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: APP_TIME_ZONE,
    year: "numeric",
  }).format(date);
}

function dayNumberInAppTimeZone(date: Date): number {
  return Math.floor((date.getTime() + SHANGHAI_OFFSET_MS) / 86400000);
}

export function truncate(str: string, maxLength: number): string {
  if (str.length <= maxLength) return str;
  return str.slice(0, maxLength - 1) + "\u2026";
}

/** Extract joined text from an array of PartData (e.g. message.parts.map(p => p.data)). */
export function extractTextFromParts(parts: PartData[]): string {
  return parts
    .filter((p): p is TextPart => p.type === "text")
    .map((p) => p.text)
    .join("\n");
}

/** Extract joined text from PartResponse[] (API response shape with nested .data). */
export function extractTextFromPartResponses(parts: Array<{ data: PartData }>): string {
  return parts
    .filter((p) => p.data.type === "text")
    .map((p) => (p.data as TextPart).text)
    .join("\n");
}

export type SessionTimestampSort = "created" | "updated";

export function getSessionTimestamp<
  T extends { time_created?: string; time_updated: string },
>(session: T, sortBy: SessionTimestampSort): string {
  return sortBy === "created"
    ? session.time_created ?? session.time_updated
    : session.time_updated;
}

export function groupSessionsByDate<
  T extends { time_created?: string; time_updated: string },
>(
  sessions: T[],
  sortBy: SessionTimestampSort = "updated",
  nowDate: Date = new Date(),
): { label: string; sessions: T[] }[] {
  const today = dayNumberInAppTimeZone(nowDate);

  const groups: Record<string, T[]> = {
    today: [],
    yesterday: [],
    previous7Days: [],
    previous30Days: [],
    older: [],
  };

  for (const session of sessions) {
    const timestamp = getSessionTimestamp(session, sortBy);
    const daysAgo = today - dayNumberInAppTimeZone(parseBackendDate(timestamp));
    if (daysAgo <= 0) groups["today"].push(session);
    else if (daysAgo === 1) groups["yesterday"].push(session);
    else if (daysAgo < 7) groups["previous7Days"].push(session);
    else if (daysAgo < 30) groups["previous30Days"].push(session);
    else groups["older"].push(session);
  }

  return Object.entries(groups)
    .filter(([, items]) => items.length > 0)
    .map(([label, items]) => ({ label, sessions: items }));
}

export interface WorkspaceGroup<T> {
  directory: string;
  label: string;
  sessions: T[];
}

export function normalizeDirectory(directory: string): string {
  const normalized = directory.replace(/\\/g, "/");
  if (/^\/+$/u.test(normalized)) return "/";
  if (/^[A-Za-z]:\/+$/u.test(normalized)) return `${normalized.slice(0, 2)}/`;
  return normalized.replace(/\/+$/, "");
}

export function directoryLabelOf(directory: string): string {
  const normalized = normalizeDirectory(directory);
  return normalized.split("/").pop() || normalized;
}

export function groupSessionsByWorkspace<T extends { directory: string | null }>(
  sessions: T[],
): { projects: WorkspaceGroup<T>[]; chats: T[] } {
  const projects = new Map<string, WorkspaceGroup<T>>();
  const chats: T[] = [];
  for (const s of sessions) {
    if (!s.directory || s.directory === ".") {
      chats.push(s);
      continue;
    }
    const dir = normalizeDirectory(s.directory);
    const existing = projects.get(dir);
    if (existing) {
      existing.sessions.push(s);
    } else {
      projects.set(dir, { directory: dir, label: directoryLabelOf(dir), sessions: [s] });
    }
  }
  return { projects: Array.from(projects.values()), chats };
}
