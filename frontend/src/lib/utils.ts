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

export function formatRelativeTime(date: string | Date, nowDate: Date = new Date()): string {
  const d = parseBackendDate(date);
  const diff = Math.max(0, nowDate.getTime() - d.getTime());
  const seconds = Math.floor(diff / 1000);
  const minutes = Math.floor(seconds / 60);
  const hours = Math.floor(minutes / 60);
  const days = Math.floor(hours / 24);

  if (seconds < 60) return "刚刚";
  if (minutes < 60) return `${minutes}分钟前`;
  if (hours < 24) return `${hours}小时前`;
  if (days < 7) return `${days}天前`;
  if (days < 30) return `${Math.floor(days / 7)}周前`;

  return d.toLocaleDateString("zh-CN", {
    timeZone: APP_TIME_ZONE,
    month: "short",
    day: "numeric",
    year: yearInAppTimeZone(d) !== yearInAppTimeZone(nowDate) ? "numeric" : undefined,
  });
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

export function groupSessionsByDate<T extends { time_updated: string }>(
  sessions: T[],
): { label: string; sessions: T[] }[] {
  const today = dayNumberInAppTimeZone(new Date());

  const groups: Record<string, T[]> = {
    today: [],
    yesterday: [],
    previous7Days: [],
    previous30Days: [],
    older: [],
  };

  for (const session of sessions) {
    const daysAgo = today - dayNumberInAppTimeZone(parseBackendDate(session.time_updated));
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
