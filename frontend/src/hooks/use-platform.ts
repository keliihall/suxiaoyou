"use client";

import { useEffect, useState } from "react";
import { IS_DESKTOP } from "@/lib/constants";
import { desktopAPI } from "@/lib/tauri-api";

export type Platform = "macos" | "windows" | "linux" | "unknown";

let cached: Platform | null = null;
let pending: Promise<Platform> | null = null;

function normalize(p: string): Platform {
  return p === "macos" || p === "windows" || p === "linux" ? p : "unknown";
}

function detectBrowserPlatform(): Platform {
  if (typeof navigator === "undefined") return "unknown";
  const nav = navigator as Navigator & {
    userAgentData?: { platform?: string };
  };
  const description = [nav.userAgentData?.platform, nav.platform, nav.userAgent]
    .filter(Boolean)
    .join(" ")
    .toLowerCase();
  if (/mac|darwin/.test(description)) return "macos";
  if (/win/.test(description)) return "windows";
  if (/linux|x11/.test(description)) return "linux";
  return "unknown";
}

export function usePlatform(): Platform {
  const [platform, setPlatform] = useState<Platform>(cached ?? "unknown");

  useEffect(() => {
    if (!IS_DESKTOP || cached) return;
    pending ??= desktopAPI
      .getPlatform()
      .then(normalize)
      .catch((error) => {
        console.warn("[Platform] Native lookup failed; using browser fallback", error);
        return detectBrowserPlatform();
      })
      .then((resolved) => {
        cached = resolved;
        return resolved;
      });
    pending.then(setPlatform);
  }, []);

  return platform;
}

export function useIsMacOS(): boolean {
  return usePlatform() === "macos";
}

export function useIsDesktop(): boolean {
  const [isDesktop, setIsDesktop] = useState(false);

  useEffect(() => {
    const mediaQuery = window.matchMedia("(min-width: 1024px)");
    const onChange = (event: MediaQueryListEvent) => setIsDesktop(event.matches);
    setIsDesktop(mediaQuery.matches);
    mediaQuery.addEventListener("change", onChange);
    return () => mediaQuery.removeEventListener("change", onChange);
  }, []);

  return isDesktop;
}
