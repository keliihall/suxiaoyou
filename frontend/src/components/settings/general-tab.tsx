"use client";

import { useState, useEffect } from "react";
import { Sun, Moon, Monitor, Eye, EyeOff } from "lucide-react";
import { useTheme } from "next-themes";
import { useTranslation } from "react-i18next";
import { Separator } from "@/components/ui/separator";
import { IS_DESKTOP } from "@/lib/constants";
import { TextPart } from "@/components/parts/text-part";
import { AppearanceCustomize } from "@/components/settings/appearance-customize";
import frontendPackage from "../../../package.json";

export function GeneralTab() {
  const { t, i18n } = useTranslation('settings');
  const { theme, resolvedTheme, setTheme } = useTheme();
  const [mounted, setMounted] = useState(false);
  const [appVersion, setAppVersion] = useState(frontendPackage.version);

  useEffect(() => {
    setMounted(true);
  }, []);

  useEffect(() => {
    if (!IS_DESKTOP) return;
    import("@tauri-apps/api/app").then(({ getVersion }) =>
      getVersion().then(setAppVersion)
    ).catch(() => {});
  }, []);

  const [showPreview, setShowPreview] = useState(false);
  const [proseFont, setProseFont] = useState<"serif" | "sans">("serif");
  const activeAppearance = mounted
    ? resolvedTheme === "light"
      ? t("light")
      : t("dark")
    : null;

  return (
    <div className="space-y-8">
      {/* Theme Section */}
      <section>
        <h2 className="text-ui-title-sm font-semibold text-[var(--text-primary)] mb-3">
          {t('appearance')}
        </h2>
        <div className="grid grid-cols-3 gap-2">
          {[
            { value: "light", label: t('light'), icon: Sun },
            { value: "dark", label: t('dark'), icon: Moon },
            { value: "system", label: t('system'), icon: Monitor },
          ].map(({ value, label, icon: Icon }) => (
            <button
              key={value}
              onClick={() => setTheme(value)}
              className={`flex flex-col items-center gap-2 rounded-xl border p-4 transition-colors ${
                mounted && theme === value
                  ? "border-[var(--brand-primary)] bg-[var(--brand-primary)]/5"
                  : "border-[var(--border-default)] hover:bg-[var(--surface-secondary)]"
              }`}
            >
              <Icon className="h-5 w-5" />
              <span className="text-ui-caption font-medium">{label}</span>
            </button>
          ))}
        </div>
        {activeAppearance && (
          <p className="mt-3 text-ui-caption text-[var(--text-tertiary)]">
            {theme === "system"
              ? t("appearanceActiveSystem", { appearance: activeAppearance })
              : t("appearanceActive", { appearance: activeAppearance })}
          </p>
        )}

        {/* 排版预览 */}
        <button
          onClick={() => setShowPreview(!showPreview)}
          className="mt-3 flex items-center gap-1.5 text-ui-caption text-[var(--text-tertiary)] hover:text-[var(--text-secondary)] transition-colors"
        >
          {showPreview ? <EyeOff className="h-3.5 w-3.5" /> : <Eye className="h-3.5 w-3.5" />}
          {t("typographyPreview")}
        </button>
        <div className="mt-6">
          <AppearanceCustomize />
        </div>

        {showPreview && (
          <>
            <div className="mt-3 grid grid-cols-2 gap-2">
              {([
                { value: "serif", label: t("serifFont") },
                { value: "sans", label: t("sansSerifFont") },
              ] as const).map(({ value, label }) => (
                <button
                  key={value}
                  onClick={() => setProseFont(value)}
                  className={`rounded-lg border px-3 py-2 text-ui-caption font-medium transition-colors ${
                    proseFont === value
                      ? "border-[var(--brand-primary)] bg-[var(--brand-primary)]/5"
                      : "border-[var(--border-default)] hover:bg-[var(--surface-secondary)]"
                  }`}
                >
                  {label}
                </button>
              ))}
            </div>
            <div
              className="mt-3 rounded-xl border border-[var(--border-default)] bg-[var(--surface-chat)] p-5 overflow-y-auto max-h-[70vh]"
              style={{ ["--prose-font" as string]: PROSE_FONT_STACKS[proseFont] }}
            >
              <TextPart data={{ type: "text", text: TYPOGRAPHY_SAMPLE }} />
            </div>
          </>
        )}
      </section>

      <Separator />

      {/* Language Section */}
      <section>
        <h2 className="text-ui-title-sm font-semibold text-[var(--text-primary)] mb-3">
          {t('language')}
        </h2>
        <div className="grid grid-cols-2 gap-2">
          {[
            { value: "en", label: t("languageEnglish") },
            { value: "zh", label: t("languageChinese") },
          ].map(({ value, label }) => (
            <button
              key={value}
              onClick={() => {
                i18n.changeLanguage(value);
                localStorage.setItem("suxiaoyou-language", value);
              }}
              className={`flex flex-col items-center gap-2 rounded-xl border p-4 transition-colors ${
                mounted && i18n.language.startsWith(value)
                  ? "border-[var(--brand-primary)] bg-[var(--brand-primary)]/5"
                  : "border-[var(--border-default)] hover:bg-[var(--surface-secondary)]"
              }`}
            >
              <span className="text-ui-caption font-medium">{label}</span>
            </button>
          ))}
        </div>
      </section>

      <Separator />

      {/* About */}
      <section>
        <h2 className="text-ui-title-sm font-semibold text-[var(--text-primary)] mb-3">
          {t('about')}
        </h2>
        <div className="text-ui-caption text-[var(--text-secondary)] space-y-1">
          <p>{t('aboutVersion', { version: appVersion })}</p>
          <p>{t('aboutDesc')}</p>
          <p>{t('aboutCopyright')}</p>
        </div>
      </section>
    </div>
  );
}

const PROSE_FONT_STACKS = {
  serif: 'ui-serif, Georgia, Cambria, "Times New Roman", Times, serif',
  sans: '"Inter", "Noto Sans SC", ui-sans-serif, system-ui, sans-serif',
} as const;

const TYPOGRAPHY_SAMPLE = `# 一级标题

这是一段用于提供上下文的导语。它应该和下方内容保持舒适间距，上方标题也应该像清晰的章节起点。

## 二级标题 — 章节标题

这一段跟在二级标题后面，应该靠近标题，形成完整的内容组，而不是漂浮在页面中间。

这是第二段正文。连续段落之间要有稳定的节奏：彼此相关，但不拥挤。

### 三级标题 — 功能列表

列表应该结构清楚、便于扫描：

- **第一项** — 使用加粗开头并补充说明
- 第二项包含一个[链接示例](https://example.com)
- 第三项包含 \`行内代码\` 引用
  - 嵌套项目一
  - 嵌套项目二
    - 更深一级的项目

#### 四级小标题

有序列表应该编号清晰：

1. 安装依赖
2. 配置环境变量
3. 启动开发服务器

---

## 代码块展示

下面是一段代码示例：

\`\`\`python
def hello(name: str) -> str:
    """按名字问候某个人。"""
    return f"你好，{name}！"

# 使用示例
result = hello("苏小有")
print(result)
\`\`\`

上面的代码块应该像一个独立区域，边界清晰、质感克制。

## 表格与引用

| 能力 | 传统接口 | 灵活查询 |
|---------|------|---------|
| 入口 | 多个 | 单个 |
| 数据获取 | 容易过取或少取 | 精确字段 |
| 缓存 | 浏览器原生 | 自定义 |
| 学习成本 | 低 | 中 |

> 引用块应该有清晰的视觉边界，但不要过重。这是一段引用内容，用来测试 blockquote 的排版效果。

## 弱结构文本测试

项目名称：苏小有
类型：人工智能桌面助手
技术栈：桌面端 + 网页界面 + 本地服务
开源协议：开源许可
核心卖点：本地优先

这一段是正常长度的段落，用来测试弱结构短段落和正常段落之间的视觉过渡。上面的短段落应该收紧间距，形成一个视觉组，而不是散乱的换行。
`;
