type TranslateFn = (key: string, options?: Record<string, unknown>) => string;

interface ActivityToolPart {
  tool: string;
  state: {
    title: string | null;
    input: Record<string, unknown>;
  };
}

const TITLE_TRANSLATORS: Array<[RegExp, (match: RegExpMatchArray) => string]> = [
  [/^Loaded skill:\s*(.+)$/i, (m) => `已加载技能：${m[1]}`],
  [/^Created\s+(.+)$/i, (m) => `已创建 ${m[1]}`],
  [/^Updated\s+(.+)$/i, (m) => `已更新 ${m[1]}`],
  [/^Rewrote\s+(.+)$/i, (m) => `已重写 ${m[1]}`],
  [/^Presented\s+(.+)$/i, (m) => `已展示 ${m[1]}`],
  [/^Fetched\s+(.+)$/i, (m) => `已获取 ${m[1]}`],
  [/^Search:\s*(.+?)\s*\((\d+)\s+results?\)$/i, (m) => `搜索：${m[1]}（${m[2]} 条结果）`],
  [/^Search:\s*(.+)$/i, (m) => `搜索：${m[1]}`],
  [/^No results for\s+"(.+)"$/i, (m) => `未找到“${m[1]}”的结果`],
  [/^(\d+)\s+results?\s+for\s+"(.+)"$/i, (m) => `${m[1]} 条“${m[2]}”的搜索结果`],
  [/^(\d+)\s+files?\s+matching\s+(.+)$/i, (m) => `${m[1]} 个文件匹配 ${m[2]}`],
  [/^(\d+)\s+matches?\s+for\s+(.+)$/i, (m) => `${m[1]} 处匹配 ${m[2]}`],
  [/^Applied patch\s+\((\d+)\s+files?\)$/i, (m) => `已应用补丁（${m[1]} 个文件）`],
  [/^Found\s+(\d+)\s+tool\(s\)$/i, (m) => `找到 ${m[1]} 个工具`],
  [/^Found\s+(\d+)\s+tools?$/i, (m) => `找到 ${m[1]} 个工具`],
  [/^Tool search:\s*no results$/i, () => "工具搜索：无结果"],
  [/^SubAgent\s+\((.+?)\):\s*(.+)$/i, (m) => `子任务（${m[1]}）：${m[2]}`],
  [/^Question \(no listener\)$/i, () => "提问（无监听）"],
  [/^User answered\s+(\d+)\s+questions?$/i, (m) => `用户回答了 ${m[1]} 个问题`],
  [/^User answered:\s*(.+)$/i, (m) => `用户回答：${m[1]}`],
  [/^Plan:\s*(.+)$/i, (m) => `计划：${m[1]}`],
  [/^Plan accepted:\s*(.+)$/i, (m) => `计划已接受：${m[1]}`],
  [/^Plan saved:\s*(.+)$/i, (m) => `计划已保存：${m[1]}`],
  [/^Plan revision requested$/i, () => "已请求修改计划"],
  [/^Plan response received$/i, () => "已收到计划反馈"],
];

const PROCESS_TRANSLATORS: Array<[RegExp, string]> = [
  [
    /^The search tool seems to be having issues\.\s*Let me try using web_fetch to access some known sources about this project\.?$/i,
    "搜索工具似乎不稳定，改用 web_fetch 访问相关来源。",
  ],
  [
    /^Search is failing\.\s*Let me try using web_fetch on some known URLs about this topic\.?$/i,
    "搜索请求失败，改用 web_fetch 访问相关网址继续核验。",
  ],
  [
    /^Search is failing\.\s*Let me try using web_fetch on some known URLs about this topic\..*$/i,
    "搜索请求失败，改用 web_fetch 访问相关网址继续核验。",
  ],
  [
    /^Search is failing consistently\.\s*Let me try academic-specific searches and some known literature databases\.?$/i,
    "搜索连续失败，改用学术数据库和定向关键词继续查找。",
  ],
  [
    /^Search seems to have issues\.\s*Let me try some alternative approaches.*$/i,
    "搜索不稳定，改用学术网站和替代关键词继续检索。",
  ],
  [
    /^Let me now search for academic papers related to this project\.\s*Let me try searching in academic databases\.?$/i,
    "继续查找该项目相关论文和学术成果。",
  ],
  [
    /^The fetch returned limited content\.\s*Let me try searching with different queries.*$/i,
    "抓取到的内容有限，改用不同关键词继续检索相关资料。",
  ],
  [/^Let me inspect the implementation\.?$/i, "正在检查实现细节。"],
  [/^Let me load the (.+?) skill first.*$/i, "正在加载相关技能。"],
  [/^The user wants me to .+$/i, "正在确认用户需求和交付目标。"],
  [/^Now I have the skills loaded\..*$/i, "技能已加载，开始规划具体步骤。"],
  [/^Actually,\s*let me plan this out:?$/i, "先整理执行计划："],
  [/^Let me start by:?$/i, "先从以下步骤开始："],
  [/^Let me also .+$/i, "继续补充必要准备。"],
  [/^Let me try .+web_fetch.+$/i, "尝试通过 web_fetch 访问相关来源。"],
  [/^Let me try .+web_search.+$/i, "尝试通过 web_search 检索相关资料。"],
  [/^Let me try .+$/i, "尝试换一种方式继续处理。"],
  [/^I need to .+$/i, "正在确认下一步需要处理的事项。"],
  [/^I should .+$/i, "正在判断下一步操作。"],
  [/^I'll .+$/i, "正在按当前目标继续推进。"],
  [/^Now let me .+$/i, "现在继续执行下一步。"],
];

export function translatePersistedToolTitle(title: string | null | undefined): string | null {
  if (!title) return null;
  if (title === "Todo list") return "待办清单";

  for (const [pattern, format] of TITLE_TRANSLATORS) {
    const match = title.match(pattern);
    if (match) return format(match);
  }

  return title;
}

export function getToolDisplayTitle(tool: ActivityToolPart, t: TranslateFn): string {
  const translatedTitle = translatePersistedToolTitle(tool.state.title);
  if (translatedTitle) return translatedTitle;

  const input = tool.state.input as Record<string, string | undefined>;
  switch (tool.tool) {
    case "read":
    case "write":
    case "edit":
    case "multiedit":
      return getFileName(input.file_path) ?? t("file");
    case "apply_patch":
      return t("toolApplyingPatch");
    case "bash":
      return truncate(String(input.command ?? t("toolRunCommand")), 50);
    case "glob":
      return truncate(String(input.pattern ?? "**/*"), 30);
    case "grep":
      return t("toolSearch", { query: truncate(String(input.pattern ?? ""), 30) });
    case "search":
      return t("toolSearch", { query: truncate(String(input.query ?? ""), 40) });
    case "web_search":
      return t("toolWebSearch", { query: truncate(String(input.query ?? ""), 40) });
    case "web_fetch":
      return t("toolFetch", { url: truncate(String(input.url ?? ""), 40) });
    case "task":
      return truncate(String(input.description ?? t("toolSubtask")), 40);
    case "question":
      return t("toolAskQuestionShort");
    case "todo":
      return t("toolUpdateProgress");
    default:
      return tool.tool;
  }
}

export function localizeVisibleProcessText(text: string, language?: string): string {
  if (!shouldUseChinese(language) || !text) return text;

  let inCodeFence = false;
  return text
    .split("\n")
    .map((line) => {
      if (/^\s*```/.test(line)) {
        inCodeFence = !inCodeFence;
        return line;
      }
      if (inCodeFence) return line;
      return localizeVisibleProcessLine(line);
    })
    .join("\n");
}

function localizeVisibleProcessLine(line: string): string {
  const match = line.match(/^(\s*(?:[-*•]|\d+[.)])?\s*)(.*?)(\s*)$/);
  if (!match) return line;

  const [, prefix, body, suffix] = match;
  if (!body) return line;

  const translated = translateProcessBody(body.trim());
  if (!translated && /[\u3400-\u9fff]/.test(body)) return line;
  return translated ? `${prefix}${translated}${suffix}` : line;
}

function translateProcessBody(body: string): string | null {
  for (const [pattern, replacement] of PROCESS_TRANSLATORS) {
    if (pattern.test(body)) return replacement;
  }

  if (!isEnglishProcessText(body)) return null;

  const lower = body.toLowerCase();
  if (lower.includes("academic") || lower.includes("paper") || lower.includes("literature") || lower.includes("scholar") || lower.includes("cnki")) {
    return "继续查找相关论文和学术成果。";
  }
  if (lower.includes("search") && (lower.includes("fail") || lower.includes("issue"))) {
    return "搜索不稳定，改用其他方式继续核验。";
  }
  if (lower.includes("web_fetch") || lower.includes("fetch")) {
    return "继续抓取并核验相关网页资料。";
  }
  if (lower.includes("skill")) {
    return "正在加载并应用相关技能。";
  }
  if (lower.includes("plan") || lower.includes("outline")) {
    return "正在整理执行计划。";
  }
  if (lower.includes("user wants") || lower.includes("user asked")) {
    return "正在确认用户需求和交付目标。";
  }

  return null;
}

function isEnglishProcessText(text: string): boolean {
  const letters = text.match(/[A-Za-z]/g)?.length ?? 0;
  if (letters < 8) return false;
  const cjk = text.match(/[\u3400-\u9fff]/g)?.length ?? 0;
  if (cjk > 0) return false;
  return /^(The user|The search|The fetch|Search|Let me|I need|I should|I'll|I will|I'm|Now I|Now let me|Actually)/i.test(text);
}

function shouldUseChinese(language?: string): boolean {
  return Boolean(language?.toLowerCase().startsWith("zh"));
}

function getFileName(filePath?: string): string | null {
  if (!filePath) return null;
  const parts = filePath.replace(/\\/g, "/").split("/");
  return parts[parts.length - 1];
}

function truncate(s: string, max: number): string {
  return s.length > max ? s.slice(0, max - 3) + "..." : s;
}
