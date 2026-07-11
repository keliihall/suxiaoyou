"use client";

import { useEffect } from "react";
import { TitleBar } from "@/components/desktop/title-bar";

export default function GlobalError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  useEffect(() => {
    console.error("[GlobalError]", error);
  }, [error]);

  return (
    <html lang="zh-CN">
      <body
        style={{
          margin: 0,
          color: "#1a1c1f",
          background: "#fff",
          fontFamily:
            '-apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif',
        }}
      >
        <TitleBar recoveryActive />
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
              alt="苏小有"
              style={{ display: "block", margin: "0 auto 24px" }}
            />
            <h1 style={{ margin: 0, fontSize: 22, lineHeight: 1.4 }}>
              应用界面遇到错误
            </h1>
            <p
              style={{
                margin: "12px 0 24px",
                color: "#5f6368",
                fontSize: 14,
                lineHeight: 1.7,
              }}
            >
              苏小有无法继续显示当前界面。你可以重试；若问题持续，请重新启动应用。
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
              重试
            </button>
          </div>
        </main>
      </body>
    </html>
  );
}
