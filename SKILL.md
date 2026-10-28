---
slug: sicuns-token-usage-stats
displayName: 积分转 Token 用量
version: 1.1.0
summary: 统计本机 AI 编码工具的 token 用量与积分消耗，算出花了多少钱、缓存命中率多少、高峰与非高峰各占几成，支持按模型、平台、日期下钻，可用于成本审计与模型选型对比
license: MIT
tags: [token, 用量统计, 积分, 花费, 成本, 缓存命中率, 模型对比, 成本审计]
name: token-usage-stats
description: 统计本机 AI 编码工具的 token 用量、真实积分消耗、缓存命中率与高峰/非高峰花费（支持 WorkBuddy、Codex CLI、Claude Code、QoderWork、Gemini CLI 等，其余工具走通用解析）。当用户询问“我用了多少 token”“这个月消耗多少积分”“花了多少钱”“哪个模型最烧钱”“缓存命中率多少”“各平台用量对比”时使用；也可用于成本审计与模型选型建议。
agent_created: true
---

# AI 用量统计

读取本机各 AI 编码工具留下的日志，输出真实 token、真实积分、缓存命中率与峰谷折算金额，并支持按平台、按模型、按日期下钻。

零第三方依赖（Python 3.8+ 标准库），不联网，数据不出本机。

## 何时使用

- 用户问"我用了多少 token / 消耗多少积分 / 花了多少钱"
- 用户想比较不同模型或不同工具的消耗
- 用户想做成本审计、判断该换哪个模型
- 用户想看缓存命中率、高峰/非高峰用量分布

## 执行步骤

1. **确认 Python**：优先用平台托管运行时，否则 `python3` / `python`。脚本纯标准库，无需安装依赖。
2. **首次先诊断**（换机器或换工具后必做）：

   ```
   python <skill目录>/scripts/usage_stats.py --scan
   ```

   确认哪些平台「正常」、哪些「未检测到」、哪些「无用量记录」。**未检测到的平台不要臆测数据**。

3. **跑汇总表**（默认 7 日）：

   ```
   python <skill目录>/scripts/usage_stats.py --days 7
   ```

4. **按需下钻**（每次只跑一个，避免刷屏）：

   | 目的 | 命令 |
   |---|---|
   | 近 N 日汇总 | `--days 30` |
   | 今日各模型分布 | `--date today` |
   | 指定日模型分布 | `--date 2025-03-13` |
   | 按平台对比 | `--by-source` |
   | 数据源诊断 | `--scan` |
   | 机器可读 | `--json` |
   | 只看某几个平台 | `--only workbuddy,codex` |
   | 交互菜单（人工用） | 无参数直接运行，或 `--menu` |

5. **汇报**：脚本的纯文本表格**只用于终端/黑框，不要原样贴进对话**。在对话框里改用 Markdown 重排（见下方「对话呈现规范」），再点出关键结论。

## 对话呈现规范

脚本输出面向等宽终端，直接贴进 Markdown 会歪列。**在对话中一律重新排版**，脚本本体不改动：

- 主表用 Markdown 表格，列建议：日期 / 星期 / Token / 轮次 / 积分 / 命中率 / 预估金额；Token 用「万」「亿」并保留 1–2 位小数，积分保留 1 位。
- 合计行可并入表尾一行并加粗，或表格下单列一行「**近 N 日合计：X 亿 token · Y 积分 · 命中率 Z% · ¥W**」。
- 关键指标用加粗或引用块突出，例如：`> 缓存命中率 **96.5%**，非高峰实扣 120.8 分（原价 241.6 分，5 折省下 120.8 分）`。
- 峰谷、积分折算等口径用一段小字说明，不要塞进表格。
- 下钻（模型分布）同样用 Markdown 表格，按积分降序，末尾加合计行。
- 只给用户问的那张表，不主动堆多个视图；确实需要对比时最多两张。
- 「预估金额」是按牌价折算的统一基准，**不是实际扣费**，每次都要说明。

## 数据源

| 平台 | 默认路径 | 解析方式 |
|---|---|---|
| WorkBuddy | `~/.workbuddy/projects` | 精确解析，取 `providerData.rawUsage.credit` 实扣积分 |
| Codex CLI | `~/.codex/sessions` | 精确解析，按 `last_token_usage` 增量累加 |
| Claude Code | `~/.claude/projects` | 通用解析 |
| QoderWork / Qoder | `~/.qoderworkcn/projects`、`~/.qoder/projects` | 通用解析 |
| Gemini CLI | `~/.gemini/tmp` | 通用解析 |
| Qwen Code | `~/.qwen/tmp` | 通用解析 |
| OpenCode / Crush / Goose / Aider | 见脚本 `SOURCES` | 通用解析 |

未检测到的平台自动跳过，不报错。新增平台只需在 `SOURCES` 加一行 `id/name/path`，`kind` 用 `generic`。
路径不同可用 `~/.ai-usage-stats.json` 覆盖：`{"paths": {"claude": "D:/logs"}}`，或用 `--projects` / `--codex` / `--claude`。

## 指标口径

详细说明见 `references/metrics.md`，要点：

- **积分**：`rawUsage.credit` 是服务端返回的单次调用实扣值，**已含峰谷折扣**，直接累加即可，不能再次乘 0.5。
- **峰谷**：周一至周五 09:00–12:00、14:00–18:00 原价，其余时段（含周末）5 折；按 `--tz` 指定时区判定，默认 UTC+8。
- **命中率**：缓存命中 token ÷ 总输入 token。
- **金额**：按 deepseek-v4.1-flash 牌价折算（输入 2.0 / 输出 8.0 / 缓存命中 0.04 元每百万，高峰系数 1.0、非高峰 0.5），是横向可比的统一基准，非实际扣费。

## 注意

- Codex CLI 的 `token_count` 事件必须取 `last_token_usage`（增量），用 `total_token_usage` 会重复累加。
- Claude Code / Anthropic 系日志的 `input_tokens` **不含**缓存，总输入需加上 `cache_read_input_tokens` 与 `cache_creation_input_tokens`；OpenAI 系的 `prompt_tokens` 已含缓存。通用解析器靠是否存在 `cache_*` 字段自动区分，改动时勿破坏这个判断。
- 不同模型单价差异极大（实测 glm-5.3 约为 deepseek-v4.1-flash 的 38 倍），token 多不等于烧钱，汇报时应同时给出积分而非只看 token。
- 目录存在但日志没有 token 字段时（如 QoderWork），会显示「无用量记录」，这是该工具不落盘用量，不是脚本错误，**不要编造数字**。
- 统计结果随日志实时写入而变化，同一天多次运行数字会小幅上涨。
