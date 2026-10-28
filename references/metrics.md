# Token 与积分口径说明

## 一、为什么不直接用 token 数算钱

各模型的计费倍率差得很远。实测同一账号、同一周的样本：

| 模型 | Token 占比 | 积分占比 | 分/百万 token |
|---|---|---|---|
| glm-5.3 | 4.5% | 59.7% | 53.4 |
| deepseek-v4.1-flash | 79.7% | 28.4% | 1.4 |
| mimo-v2.6-pro | 6.4% | 6.0% | 3.8 |
| glm-5.3-flash | 3.3% | 3.8% | 4.7 |
| hy4-preview | 0.4% | 2.0% | 18.6 |
| hy3 | 2.8% | 0% | 免费 |

glm-5.3 只占了 4.5% 的 token，却吃掉近六成积分，单价是 deepseek-v4.1-flash 的 38 倍。所以本工具同时给出 token 和积分两列，判断花费要看积分。

## 二、积分字段

WorkBuddy 的每条请求日志里有 `providerData.rawUsage.credit`，是服务端算好的**本次调用实扣积分**。

- 12,385 条样本，字段覆盖率 100%，无缺失。
- 极少数没有 usage 的团队消息记录，积分在 `providerData.credit` 顶层，`source=team_member`，量极小（约 16 分/累计），本工具一并计入。

## 三、峰谷折扣：不要重复打折

规则：周一至周五 09:00–12:00、14:00–18:00 为高峰原价，其余时段（含周末）5 折。

**`credit` 字段已经是折后值。** 实测同模型高峰/非高峰的单位积分：

| 模型 | 高峰(分/百万) | 非高峰(分/百万) | 比值 |
|---|---|---|---|
| deepseek-v4.1-flash | 2.05 | 1.04 | 0.51 |
| deepseek-v4-flash | 5.12 | 2.64 | 0.52 |
| glm-5.3 | 54.81 | 49.72 | 0.91 |
| glm-5.2 | 53.68 | 53.35 | 0.99 |
| kimi-k3-1 | 83.34 | 88.56 | 1.06 |

deepseek 系列正好 0.5 左右，说明服务端在返回 `credit` 时已经打过折。如果再乘 0.5，会把花费算少一半。

另外 glm / kimi 系列比值约等于 1，实际不参与峰谷折扣，按原价扣。

**结论：积分直接累加。只有"按牌价估算的金额"才需要乘峰谷系数。**

## 四、金额估算

用于给出一个"如果按 API 官方价买，大概多少钱"的参照，不是实际扣费。

按 deepseek-v4.1-flash 牌价（元/百万 token）：

| 项目 | 单价 |
|---|---|
| 输入（缓存未命中） | 2.0 |
| 输出 | 8.0 |
| 缓存命中读取 | 0.04 |
| 缓存写入 | 1.25 × 输入价 |

公式：

```
金额 = (命中 × 0.04 + 写入 × 1.25 × 2 + 未命中 × 2 + 输出 × 8) × pf / 1e6
```

`pf` 为峰谷系数：高峰 1.0，非高峰 0.5。

积分折算参考：7000 积分 = 60 元，即 1 积分 ≈ 0.008571 元。不同套餐比例不同，可自行调整脚本头部 `CREDIT_YUAN`。

## 五、各平台解析要点

### WorkBuddy
- 每条记录 `providerData.rawUsage` 含 `prompt_tokens`、`completion_tokens`、`prompt_cache_hit_tokens`、`cache_creation_input_tokens`、`credit`。
- 模型名优先取 `model`，回退 `requestModelId`（用户选的档位，如 `deep-model` 对应"极致"）。

### Codex CLI
- 记录结构为 `{type, timestamp, payload}`，用量在 `payload.type == "token_count"` 的 `info.last_token_usage`。
- **必须用 `last_token_usage`（增量）**，`total_token_usage` 是会话内累计值，用它会把总量重复算很多遍。
- 字段：`input_tokens`（含 cached）、`cached_input_tokens`、`cache_write_input_tokens`、`output_tokens`、`reasoning_output_tokens`。
- 模型名通常不落盘，统一归为 `gpt-5-codex`。

### Claude Code / 通用解析器
- 记录结构 `{type, timestamp, message}`，`type == "assistant"` 且 `message.usage` 存在时计入。
- 字段：`input_tokens`、`cache_creation_input_tokens`、`cache_read_input_tokens`、`output_tokens`。
- **注意**：Claude 的 `input_tokens` 不含缓存部分，总输入 = input + cache_creation + cache_read，与 DeepSeek 的 "prompt_tokens 已含缓存" 语义不同。

通用解析器（`collect_generic`）不看工具名，只在 JSON 里找"最像 usage"的对象：

1. 递归下降，节点上出现 `input_tokens` / `prompt_tokens` / `output_tokens` / `completion_tokens` 等字段即计分，分数最高、层级最浅的胜出。
2. 只有 `total_tokens` 而没有输入/输出结构的对象会被排除，避免把 `model_context_window` 之类的配置项当成用量。
3. **Anthropic 与 OpenAI 语义自动区分**：出现 `cache_read_input_tokens` 或 `cache_creation_input_tokens` → 输入不含缓存，需相加；否则视为 `prompt_tokens` 已含缓存。
4. **去重**：优先用 `message.id` / `requestId` / `id` / `uuid` 作为键，流式重写产生的同一条消息只算一次；无 id 时退回行号。
5. 支持 `.jsonl` 逐行与 `.json` 整文件（顶层数组按元素拆分）两种形态。

## 六、常见坑

1. **不要用 `total_tokens` 当输出量算速度**：一轮请求的 total 里九成以上是输入上下文。
2. **不要在聚合时重复计会话**：一个 jsonl 文件就是一个会话，用文件名去重。
3. **时间戳有时区**：带 `Z` 的是 UTC，无标记的按本地；本工具统一转成 `--tz` 指定时区后再判断峰谷，跨时区使用时务必调整。
4. **数字会随时间上涨**：当前对话也在实时写日志，同一天连续运行结果会小幅变化。
5. **控制台中文对不齐**：Python 的 `%s` 按字符数补齐，中文占两个显示列。本工具用 `unicodedata.east_asian_width` 按显示宽度补齐。
