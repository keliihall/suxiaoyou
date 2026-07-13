"use client";

import { useEffect, useState } from "react";
import "@/i18n/config";
import { TitleBar } from "@/components/desktop/title-bar";

export default function GlobalError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  const [language, setLanguage] = useState<"en" | "zh">("en");

  useEffect(() => {
    console.error("[GlobalError]", error);
    const stored = window.localStorage.getItem("suxiaoyou-language");
    setLanguage(
      stored === "zh" || (stored !== "en" && window.navigator.language.toLowerCase().startsWith("zh"))
        ? "zh"
        : "en",
    );
  }, [error]);

  const copy = language === "zh"
    ? {
        appName: "苏小有",
        title: "应用界面遇到错误",
        description: "苏小有无法继续显示当前界面。你可以重试；若问题持续，请重新启动应用。",
        retry: "重试",
      }
    : {
        appName: "suyo",
        title: "The app encountered an error",
        description: "suyo cannot continue displaying this screen. Try again, or restart the app if the problem continues.",
        retry: "Try again",
      };

  return (
    <html lang={language === "zh" ? "zh-CN" : "en"}>
      <body
        style={{
          margin: 0,
          color: "#1a1c1f",
          background: "#fff",
          fontFamily:
            '-apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif',
        }}
      >
        <TitleBar recoveryActive appName={copy.appName} />
        <main
          style={{
            minHeight: "100vh",
            boxSizing: "border-box",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            padding: 32,
            textAlign: "center",
          }}
        >
          <div style={{ maxWidth: 420 }}>
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img
              src="/favicon.svg"
              width={64}
              height={64}
              alt={copy.appName}
              style={{ display: "block", margin: "0 auto 24px" }}
            />
            <h1 style={{ margin: 0, fontSize: 22, lineHeight: 1.4 }}>
              {copy.title}
            </h1>
            <p
              style={{
                margin: "12px 0 24px",
                color: "#5f6368",
                fontSize: 14,
                lineHeight: 1.7,
              }}
            >
              {copy.description}
            </p>
            <button
              type="button"
              onClick={reset}
              style={{
                minHeight: 40,
                padding: "0 20px",
                border: 0,
                borderRadius: 10,
                color: "#fff",
                background: "#339cff",
                fontSize: 14,
                fontWeight: 600,
                cursor: "pointer",
              }}
            >
              {copy.retry}
            </button>
          </div>
        </main>
      </body>
    </html>
  );
}
