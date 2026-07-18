type TranslateFn = (key: string, options?: Record<string, unknown>) => string;

interface ActivityToolPart {
  tool: string;
  state: {
    title: string | null;
    input: Record<string, unknown>;
  };
}

const EN_TO_ZH_TITLE_TRANSLATORS: Array<[RegExp, (match: RegExpMatchArray) => string]> = [
  [/^Loaded skill:\s*(.+)$/i, (m) => `已加载技能：${m[1]}`],
  [/^Created\s+(.+)$/i, (m) => `已创建 ${m[1]}`],
  [/^Updated\s+(.+)$/i, (m) => `已更新 ${m[1]}`],
  [/^Rewrote\s+(.+)$/i, (m) => `已重写 ${m[1]}`],
  [
    /^Edited\s+(.+?)\s+\((\d+)\s+edits?,\s*(\d+)\s+replacements?\)$/i,
    (m) => `已编辑 ${m[1]}（${m[2]} 个编辑，${m[3]} 处替换）`,
  ],
  [
    /^Edited\s+(.+?)\s+\((\d+)\s+replacements?\)$/i,
    (m) => `已编辑 ${m[1]}（${m[2]} 处替换）`,
  ],
  [/^Presented\s+(.+)$/i, (m) => `已展示 ${m[1]}`],
  [/^Fetched\s+(.+)$/i, (m) => `已获取 ${m[1]}`],
  [/^Search:\s*(.+?)\s*\((\d+)\s+results?\)$/i, (m) => `搜索：${m[1]}（${m[2]} 条结果）`],
  [/^Search:\s*(.+)$/i, (m) => `搜索：${m[1]}`],
  [/^No results for\s+"(.+)"$/i, (m) => `未找到“${m[1]}”的结果`],
  [/^(\d+)\s+(?:search\s+)?results?\s+for\s+"(.+)"$/i, (m) => `${m[1]} 条“${m[2]}”的搜索结果`],
  [/^(\d+)\s+files?\s+match\s+(.+)$/i, (m) => `${m[1]} 个文件匹配 ${m[2]}`],
  [/^(\d+)\s+files?\s+matching\s+(.+)$/i, (m) => `${m[1]} 个文件匹配 ${m[2]}`],
  [/^(\d+)\s+matches?\s+(\/.*\/)$/i, (m) => `${m[1]} 处匹配 ${m[2]}`],
  [/^(\d+)\s+matches?\s+for\s+(.+)$/i, (m) => `${m[1]} 处匹配 ${m[2]}`],
  [/^Applied patch\s+\((\d+)\s+files?\)$/i, (m) => `已应用补丁（${m[1]} 个文件）`],
  [/^Found\s+(\d+)\s+tool\(s\)$/i, (m) => `找到 ${m[1]} 个工具`],
  [/^Found\s+(\d+)\s+tools?$/i, (m) => `找到 ${m[1]} 个工具`],
  [/^Tool search:\s*no results$/i, () => "工具搜索：无结果"],
  [/^Tool search$/i, () => "工具搜索"],
  [/^Subtask\s+\((.+?)\):\s*(.+)$/i, (m) => `子任务（${m[1]}）：${m[2]}`],
  [/^SubAgent\s+\((.+?)\):\s*(.+)$/i, (m) => `子任务（${m[1]}）：${m[2]}`],
  [
    /^Listed\s+(\d+)\s+entries?\s+in\s+(.+)$/i,
    (m) => `已列出 ${m[2]} 中的 ${m[1]} 个条目`,
  ],
  [/^Question \(no listener\)$/i, () => "提问（无监听）"],
  [/^User answered\s+(\d+)\s+questions?$/i, (m) => `用户回答了 ${m[1]} 个问题`],
  [/^User answered:\s*(.+)$/i, (m) => `用户回答：${m[1]}`],
  [/^Plan:\s*(.+)$/i, (m) => `计划：${m[1]}`],
  [/^Plan accepted:\s*(.+)$/i, (m) => `计划已接受：${m[1]}`],
  [/^Plan saved:\s*(.+)$/i, (m) => `计划已保存：${m[1]}`],
  [/^Plan revision requested$/i, () => "已请求修改计划"],
  [/^Plan (?:response|feedback) received$/i, () => "已收到计划反馈"],
];

const ZH_TO_EN_TITLE_TRANSLATORS: Array<[RegExp, (match: RegExpMatchArray) => string]> = [
  [/^已加载技能：\s*(.+)$/, (m) => `Loaded skill: ${m[1]}`],
  [/^已创建\s+(.+)$/, (m) => `Created ${m[1]}`],
  [/^已更新\s+(.+)$/, (m) => `Updated ${m[1]}`],
  [/^已重写\s+(.+)$/, (m) => `Rewrote ${m[1]}`],
  [
    /^已编辑\s+(.+?)（(\d+)\s*个编辑，(\d+)\s*处替换）$/,
    (m) => `Edited ${m[1]} (${m[2]} edits, ${m[3]} replacements)`,
  ],
  [
    /^已编辑\s+(.+?)（(\d+)\s*处替换）$/,
    (m) => `Edited ${m[1]} (${m[2]} replacements)`,
  ],
  [/^已展示\s+(.+)$/, (m) => `Presented ${m[1]}`],
  [/^已获取\s+(.+)$/, (m) => `Fetched ${m[1]}`],
  [/^搜索：\s*(.+?)（(\d+)\s*条结果）$/, (m) => `Search: ${m[1]} (${m[2]} results)`],
  [/^搜索：\s*(.+)$/, (m) => `Search: ${m[1]}`],
  [/^未找到“(.+)”的结果$/, (m) => `No results for "${m[1]}"`],
  [/^(\d+)\s*条“(.+)”的搜索结果$/, (m) => `${m[1]} search results for "${m[2]}"`],
  [/^(\d+)\s*个文件匹配\s+(.+)$/, (m) => `${m[1]} files match ${m[2]}`],
  [/^(\d+)\s*处匹配\s+(\/.*\/)$/, (m) => `${m[1]} matches ${m[2]}`],
  [/^(\d+)\s*处匹配\s+(.+)$/, (m) => `${m[1]} matches for ${m[2]}`],
  [/^已应用补丁（(\d+)\s*个文件）$/, (m) => `Applied patch (${m[1]} files)`],
  [/^找到\s+(\d+)\s*个工具$/, (m) => `Found ${m[1]} tools`],
  [/^工具搜索：无结果$/, () => "Tool search: no results"],
  [/^工具搜索$/, () => "Tool search"],
  [/^子任务（(.+?)）：\s*(.+)$/, (m) => `Subtask (${m[1]}): ${m[2]}`],
  [
    /^已列出\s+(.+?)\s+中的\s+(\d+)\s*个条目$/,
    (m) => `Listed ${m[2]} entries in ${m[1]}`,
  ],
  [/^提问（无监听）$/, () => "Question (no listener)"],
  [/^用户回答了\s+(\d+)\s*个问题$/, (m) => `User answered ${m[1]} questions`],
  [/^用户回答：\s*(.+)$/, (m) => `User answered: ${m[1]}`],
  [/^计划：\s*(.+)$/, (m) => `Plan: ${m[1]}`],
  [/^计划已接受：\s*(.+)$/, (m) => `Plan accepted: ${m[1]}`],
  [/^计划已保存：\s*(.+)$/, (m) => `Plan saved: ${m[1]}`],
  [/^已请求修改计划$/, () => "Plan revision requested"],
  [/^已收到计划反馈$/, () => "Plan feedback received"],
];

const PROCESS_TRANSLATORS: Array<[RegExp, string]> = [
  [
    /^The search tool seems to be having issues\.\s*Let me try using web_fetch to access some known sources about this project\.?$/i,
    "搜索工具似乎不稳定，改用 web_fetch 访问相关来源。",
  ],
  [
    /^Search is failing\.\s*Let me try using web_fetch on some known URLs about this topic\.?$/i,
    "搜索暂未返回可用结果，改用 web_fetch 访问相关网址继续核验。",
  ],
  [
    /^Search is failing\.\s*Let me try using web_fetch on some known URLs about this topic\..*$/i,
    "搜索暂未返回可用结果，改用 web_fetch 访问相关网址继续核验。",
  ],
  [
    /^Search is failing consistently\.\s*Let me try academic-specific searches and some known literature databases\.?$/i,
    "多次搜索未返回可用结果，改用学术数据库和定向关键词继续查找。",
  ],
  [
    /^The web search failed for some queries\.\s*Let me fetch more details from specific articles\.?$/i,
    "部分检索未返回可用结果，改用具体来源继续核验。",
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

export function translatePersistedToolTitle(
  title: string | null | undefined,
  language: string,
): string | null {
  if (!title) return null;
  const isChinese = shouldUseChinese(language);
  if (title === "Todo list" && isChinese) return "待办清单";
  if (title === "待办清单" && !isChinese) return "Todo list";

  const translators = isChinese
    ? EN_TO_ZH_TITLE_TRANSLATORS
    : ZH_TO_EN_TITLE_TRANSLATORS;
  for (const [pattern, format] of translators) {
    const match = title.match(pattern);
    if (match) return format(match);
  }

  return title;
}

export function getToolDisplayTitle(
  tool: ActivityToolPart,
  t: TranslateFn,
  language: string,
): string {
  const translatedTitle = translatePersistedToolTitle(tool.state.title, language);
  if (translatedTitle) return translatedTitle;

  const input = tool.state.input as Record<string, string | undefined>;
  switch (tool.tool) {
    case "read":
    case "write":
    case "edit":
    case "office":
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

export function translatePersistedToolOutput(
  tool: string,
  output: string | null | undefined,
  language: string,
): string | null {
  if (!output) return null;
  const isChinese = shouldUseChinese(language);

  if (tool === "todo") {
    const english = output.match(
      /^Todo list updated: (\d+)\/(\d+) completed(?:, (\d+) in progress)?(?:, (\d+) pending)?$/,
    );
    if (english && isChinese) {
      return `待办清单已更新：已完成 ${english[1]}/${english[2]}${english[3] ? `，${english[3]} 个进行中` : ""}${english[4] ? `，${english[4]} 个待处理` : ""}`;
    }
    const chinese = output.match(
      /^待办清单已更新：已完成 (\d+)\/(\d+)(?:，(\d+) 个进行中)?(?:，(\d+) 个待处理)?$/,
    );
    if (chinese && !isChinese) {
      return `Todo list updated: ${chinese[1]}/${chinese[2]} completed${chinese[3] ? `, ${chinese[3]} in progress` : ""}${chinese[4] ? `, ${chinese[4]} pending` : ""}`;
    }
  }

  if (tool === "write") {
    const english = output.match(/^(Created|Updated) (.+) \((\d+) lines\)$/);
    if (english && isChinese) {
      return `${english[1] === "Created" ? "已创建" : "已更新"} ${english[2]}（${english[3]} 行）`;
    }
    const chinese = output.match(/^(已创建|已更新) (.+)（(\d+) 行）$/);
    if (chinese && !isChinese) {
      return `${chinese[1] === "已创建" ? "Created" : "Updated"} ${chinese[2]} (${chinese[3]} lines)`;
    }
    const englishError = output.match(/^Permission denied writing: ([\s\S]+)$/);
    if (englishError && isChinese) return `没有权限写入：${englishError[1]}`;
    const chineseError = output.match(/^没有权限写入：([\s\S]+)$/);
    if (chineseError && !isChinese) {
      return `Permission denied writing: ${chineseError[1]}`;
    }
  }

  if (tool === "web_search") {
    if (output === "No results found." && isChinese) return "未找到结果。";
    if (output === "未找到结果。" && !isChinese) return "No results found.";
  }

  if (tool === "question") {
    const englishSingle = output.match(/^\[No user connected\] Asked: ([\s\S]*)$/);
    if (englishSingle && isChinese) {
      return `[没有用户连接] 已提问：${englishSingle[1]}`;
    }
    const chineseSingle = output.match(/^\[没有用户连接\] 已提问：([\s\S]*)$/);
    if (chineseSingle && !isChinese) {
      return `[No user connected] Asked: ${chineseSingle[1]}`;
    }
    const englishMultiple = output.match(
      /^\[No user connected\] \[Multiple questions\] (\d+) questions$/,
    );
    if (englishMultiple && isChinese) {
      return `[没有用户连接] [多问题] ${englishMultiple[1]} 个问题`;
    }
    const chineseMultiple = output.match(
      /^\[没有用户连接\] \[多问题\] (\d+) 个问题$/,
    );
    if (chineseMultiple && !isChinese) {
      return `[No user connected] [Multiple questions] ${chineseMultiple[1]} questions`;
    }
    if (output === "(The user did not respond within 5 minutes)" && isChinese) {
      return "（用户在 5 分钟内没有回复）";
    }
    if (output === "（用户在 5 分钟内没有回复）" && !isChinese) {
      return "(The user did not respond within 5 minutes)";
    }
    if (output === "Question timed out: the user did not respond" && isChinese) {
      return "提问超时：用户未回复";
    }
    if (output === "提问超时：用户未回复" && !isChinese) {
      return "Question timed out: the user did not respond";
    }
  }

  if (tool === "plan") {
    const pairs: Array<[string, string]> = [
      [
        "Switched to plan mode. Only read-only analysis and planning are available. Use plan(command='exit') to return to build mode when ready to implement.",
        "已切换到计划模式。现在只能只读分析和规划。准备实施时，使用 plan(command='exit') 返回构建模式。",
      ],
      [
        "Switched to build mode; full tool access has been restored.",
        "已切换到构建模式，完整工具权限已恢复。",
      ],
      ["Already in plan mode.", "已经处于计划模式。"],
      ["Not currently in plan mode.", "当前不在计划模式。"],
    ];
    for (const [english, chinese] of pairs) {
      if (output === english && isChinese) return chinese;
      if (output === chinese && !isChinese) return english;
    }
  }

  if (tool === "submit_plan") {
    const english = output.match(/^\[No user connected\] Submitted plan: ([\s\S]*)$/);
    if (english && isChinese) {
      return `[没有用户连接] 已提交计划：${english[1]}`;
    }
    const chinese = output.match(/^\[没有用户连接\] 已提交计划：([\s\S]*)$/);
    if (chinese && !isChinese) {
      return `[No user connected] Submitted plan: ${chinese[1]}`;
    }
    if (output === "(The user did not respond within 10 minutes)" && isChinese) {
      return "（用户在 10 分钟内没有回复）";
    }
    if (output === "（用户在 10 分钟内没有回复）" && !isChinese) {
      return "(The user did not respond within 10 minutes)";
    }
    const englishAccepted = output.match(
      /^The user accepted the plan \(mode: (.+?)\)\. Switch to build mode and execute it:\n\n([\s\S]*)$/,
    );
    if (englishAccepted && isChinese) {
      return `用户已接受计划（模式：${englishAccepted[1]}）。切换到构建模式并执行计划：\n\n${englishAccepted[2]}`;
    }
    const chineseAccepted = output.match(
      /^用户已接受计划（模式：(.+?)）。切换到构建模式并执行计划：\n\n([\s\S]*)$/,
    );
    if (chineseAccepted && !isChinese) {
      return `The user accepted the plan (mode: ${chineseAccepted[1]}). Switch to build mode and execute it:\n\n${chineseAccepted[2]}`;
    }
    const stopEnglish =
      "The user chose to stop and review the saved plan. Do not continue; wait for the user's next message.";
    const stopChinese =
      "用户选择停止并自行审查计划。计划已保存。不要继续执行，等待用户下一条消息。";
    if (output === stopEnglish && isChinese) return stopChinese;
    if (output === stopChinese && !isChinese) return stopEnglish;
    const englishRevision = output.match(
      /^The user requested plan revisions\.\nFeedback: ([\s\S]*?)\n\nRevise the plan and call submit_plan again\.$/,
    );
    if (englishRevision && isChinese) {
      return `用户要求修改计划。\n反馈：${englishRevision[1]}\n\n请根据反馈修改计划，并再次调用 submit_plan。`;
    }
    const chineseRevision = output.match(
      /^用户要求修改计划。\n反馈：([\s\S]*?)\n\n请根据反馈修改计划，并再次调用 submit_plan。$/,
    );
    if (chineseRevision && !isChinese) {
      return `The user requested plan revisions.\nFeedback: ${chineseRevision[1]}\n\nRevise the plan and call submit_plan again.`;
    }
    const englishFeedback = output.match(
      /^User feedback: ([\s\S]*?)\n\nRevise the plan and call submit_plan again\.$/,
    );
    if (englishFeedback && isChinese) {
      return `用户反馈：${englishFeedback[1]}\n\n请修改计划并再次调用 submit_plan。`;
    }
    const chineseFeedback = output.match(
      /^用户反馈：([\s\S]*?)\n\n请修改计划并再次调用 submit_plan。$/,
    );
    if (chineseFeedback && !isChinese) {
      return `User feedback: ${chineseFeedback[1]}\n\nRevise the plan and call submit_plan again.`;
    }
  }

  if (tool === "task") {
    if (output === "(The subtask produced no text output)" && isChinese) {
      return "（子任务没有产生文本输出）";
    }
    if (output === "（子任务没有产生文本输出）" && !isChinese) {
      return "(The subtask produced no text output)";
    }
    if (isChinese) {
      return output
        .replace("\n\n--- Key tool results ---\n", "\n\n--- 关键工具结果 ---\n")
        .replace(/\n\[Errors: ([\s\S]*)\]$/, "\n[错误：$1]");
    }
    return output
      .replace("\n\n--- 关键工具结果 ---\n", "\n\n--- Key tool results ---\n")
      .replace(/\n\[错误：([\s\S]*)\]$/, "\n[Errors: $1]");
  }

  if (tool === "apply_patch") {
    const splitAt = output.indexOf("\n\n");
    const summary = splitAt >= 0 ? output.slice(0, splitAt) : output;
    const remainder = splitAt >= 0 ? output.slice(splitAt) : "";
    const localizedSummary = summary
      .split("\n")
      .map((line) => {
        if (isChinese) {
          return line
            .replace(/^\+ Added (.+)$/, "+ 已新增 $1")
            .replace(/^- Deleted (.+)$/, "- 已删除 $1")
            .replace(/^~ Updated (.+)$/, "~ 已更新 $1");
        }
        return line
          .replace(/^\+ 已新增 (.+)$/, "+ Added $1")
          .replace(/^- 已删除 (.+)$/, "- Deleted $1")
          .replace(/^~ 已更新 (.+)$/, "~ Updated $1");
      })
      .join("\n");
    return localizedSummary + remainder;
  }

  if (tool === "artifact") {
    const patterns: Array<[RegExp, (match: RegExpMatchArray) => string, RegExp, (match: RegExpMatchArray) => string]> = [
      [
        /^Created artifact "([\s\S]+)"\.$/,
        (m) => `已创建制品“${m[1]}”。`,
        /^已创建制品“([\s\S]+)”。$/,
        (m) => `Created artifact "${m[1]}".`,
      ],
      [
        /^Updated artifact "([\s\S]+)" \(replaced (\d+) characters\)\.$/,
        (m) => `已更新制品“${m[1]}”（替换 ${m[2]} 个字符）。`,
        /^已更新制品“([\s\S]+)”（替换 (\d+) 个字符）。$/,
        (m) => `Updated artifact "${m[1]}" (replaced ${m[2]} characters).`,
      ],
      [
        /^Rewrote artifact "([\s\S]+)"\.$/,
        (m) => `已重写制品“${m[1]}”。`,
        /^已重写制品“([\s\S]+)”。$/,
        (m) => `Rewrote artifact "${m[1]}".`,
      ],
    ];
    for (const [englishPattern, toChinese, chinesePattern, toEnglish] of patterns) {
      const match = output.match(isChinese ? englishPattern : chinesePattern);
      if (match) return isChinese ? toChinese(match) : toEnglish(match);
    }
  }

  if (tool === "present_file") {
    const english = output.match(/^Presented ([\s\S]+)$/);
    if (english && isChinese) return `已展示 ${english[1]}`;
    const chinese = output.match(/^已展示 ([\s\S]+)$/);
    if (chinese && !isChinese) return `Presented ${chinese[1]}`;
  }

  return output;
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
    return "检索暂未返回可用结果，已改用其他方式继续核验。";
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
  return /^(The user|The (?:web )?search|The fetch|Search|Let me|I need|I should|I'll|I will|I'm|Now I|Now let me|Actually)/i.test(text);
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
