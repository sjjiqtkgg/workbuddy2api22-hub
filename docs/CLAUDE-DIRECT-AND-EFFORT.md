# Claude 桌面版直连与推理强度归一化

两个「客户端只肯发某种名字 / 某个档位，上游一个都不认」的缺口。以前靠本地转发层
（`wb-bridge.js`）改名补上，现在网关自己解析，客户端可以直连。

- 涉及模块：`wb_client_aliases.py`（新增）、`wb_settings.py`、`wb_proxy.py`
- 入口：`/v1/messages`、`/v1/messages/count_tokens`、`/v1/responses`、
  `/v1/chat/completions`（四条都过同一处入站钩子）
- 测试：`tests/_test_claude_model_alias.py`、`tests/_test_effort_alias.py`

---

## 1. `claude-*` 模型名

### 现象

Claude 桌面版的「第三方推理」只接受形如 `claude-*` 的模型 ID。填真实 ID
（`deepseek-v4.1-flash`）会提示「无效: 模型列表」；填 `claude-sonnet-5` 能过客户端
校验，但上游不认识：

```
POST /v1/messages  {"model":"claude-sonnet-5", ...}
-> 400  upstream 400: {"code":11102,"msg":"model [claude-sonnet-5] service info not found"}
```

上游**确实**有 `sonnet` / `opus` / `haiku` 这几个短名（`/v1/models` 里有），但
`claude-` 前缀的一个都没有。

### 映射表

| 客户端发（前缀 / 家族） | 默认映射到 |
|---|---|
| `claude-opus*` | `deepseek-v4-pro` |
| `claude-sonnet*` | `deepseek-v4.1-flash` |
| `claude-haiku*` | `deepseek-v4.1-flash` |

两条匹配规则，按顺序：

1. **前缀匹配**：`claude-sonnet-4-5` 命中 `claude-sonnet-4-5-20250929`；最长前缀优先，
   所以版本级条目胜过家族条目；
2. **家族词匹配**：Claude 3.x 的 ID 把版本写在前面（`claude-3-5-sonnet-20241022`、
   `claude-3-opus-20240229`），所以家族名出现在 `claude` 之后任意 `-` 分段里也算命中。

大小写不敏感（`claude-Sonnet-5` 与 `claude-sonnet-5` 等价）。不以 `claude` 开头的
名字一律不碰。

> 默认目标是本部署 `/v1/models` 里真实存在的 ID。上游把 `deepseek-flash` 改名成
> `deepseek-v4.1-flash` 之后，这个默认值跟着改（同一支模型）；填旧名会拿到上游 400
> `11102`，不会静默兜底。

### 配置（优先级：settings.json > 环境变量 > 内置默认）

默认关闭。设置 `WB_CLAUDE_ALIASES_ENABLED=1` 或 `claude_aliases_enabled: true` 开启；环境变量可只覆盖其中一项：

```bash
CLAUDE_ALIAS_OPUS=deepseek-v4-pro
CLAUDE_ALIAS_SONNET=deepseek-v4.1-flash
CLAUDE_ALIAS_HAIKU=deepseek-v4.1-flash
```

整表替换（`prefix=target` 或 JSON 对象都认）：

```bash
WB_CLAUDE_ALIASES="claude-sonnet=glm-5.3,claude-opus-4-5=kimi-k3.1-pro"
WB_CLAUDE_ALIASES='{"claude-haiku":"hy4flash"}'
WB_CLAUDE_ALIASES=off          # 关掉整套映射
```

看板 / 设置接口（`POST /settings`，字段名同名）：

```json
{
  "claude_aliases": {"claude-sonnet": "deepseek-v4.1-flash"},
  "claude_aliases_enabled": true
}
```

`GET /settings` 返回的是**生效值**（环境变量与内置默认已合并），面板直接展示即可。

### 行为约定

| 项 | 行为 |
|---|---|
| 回显的 `model` | **真实模型 ID**（`deepseek-v4.1-flash`）。回显 `claude-*` 会让人以为真是 Claude 答的 |
| 精确别名表（`model_aliases`） | 仍然先查、仍然回显别名本身；两张表互不影响 |
| API Key 的模型白名单 | 仍按客户端发来的名字检查，再按解析后的名字检查（两者任一通过即可）。所以 Key 白名单里写 `claude-*` 也能放行 |
| 未知模型 | 不映射，照旧打上游并拿到 400。这条是配置错误的报警器，不做静默兜底 |
| `/v1/models` | 结构不变，不追加 `claude-*` 条目（桌面端不读它） |

### 日志

```
alias: claude model claude-sonnet-5 -> deepseek-v4.1-flash
```

---

## 2. 推理强度（effort）归一化

### 现象

Codex 桌面端的滑杆只有 `low / medium / high / xhigh`，最高档发出去是 `xhigh`；
Claude 桌面端用 `thinking.budget_tokens >= 32000` 表达同一件事，网关把它翻成
`xhigh`（`anthropic_effort_from_messages`）。而本部署的 DeepSeek 构建
（`deepseek-v4.1-flash`）只发布 `low / high / max`。

不管的话有两条路，两条都不对：上游直接 400；或者被 `map_reasoning_effort` 当成
「模型不支持的档位」改写成**模型默认档** `high` —— 客户端要求「比滑杆最高还高」，
拿回来的是中间档。

### 归一化表

| 客户端发 | 上游收到 |
|---|---|
| `xhigh` | `max` |
| `ultra` | `max` |
| `low` / `medium` / `high` / `max` / `none` | 原样 |

**模型自己声明了该档位就保持原样**：`deepseek-v4-pro` 发布的是 `high / xhigh`，把它的
`xhigh` 改成 `max` 反而会被 catalog 兜底打到 `high`，比客户端要求更弱。所以只有
「模型不支持这个档位」时才改写。

处理三处字段：Responses 的 `reasoning.effort`、Chat Completions 的
`reasoning_effort` / `reasoningEffort`。Anthropic 的 `thinking.budget_tokens` 不需要
改字段，它先被翻成档位（见下表），再走同一张归一化表。

### 配置（优先级同上）

默认关闭。设置 `WB_EFFORT_ALIASES_ENABLED=1` 或 `effort_aliases_enabled: true` 开启。

```bash
WB_EFFORT_ALIASES="xhigh:max,ultra:max"   # 或 JSON；整表替换
WB_EFFORT_ALIASES=off                     # 保持客户端原值，逐字节透传
```

```json
{
  "effort_aliases": {"xhigh": "max"},
  "effort_aliases_enabled": true
}
```

### Anthropic `budget_tokens` 与档位的对应

Claude 桌面端没有 effort 字段，用预算表达强度。网关的换算（
`wb_client_aliases.THINKING_BUDGET_BANDS`）：

| `budget_tokens` | 换算档位 | 最终上游档位 |
|---|---|---|
| `< 8000` | `low` | `low` |
| `8000 – 15999` | `medium` | 没有模型声明 `medium` → catalog 兜底 `high` |
| `16000 – 31999` | `high` | `high` |
| `>= 32000` | `xhigh` | `max`（归一化表） |

取舍：不去反推 Claude「medium」对应的精确 token 数；按客户端实际会发的四段换算，
把最高一档交给归一化表处理。预算字段本身不改写，只跟着换算出来的档位走。

### 日志

```
effort: model=deepseek-v4.1-flash effort_from=xhigh effort_to=max key=国内版
```

只在真的改写时打一行，所以「后台到底收到什么」可以直接从日志回答，不用抓包。

---

## 3. 客户端直连配置

**Claude 桌面版** → 推理配置：

| 字段 | 值 |
|---|---|
| 基础 URL | `http://<网关地址>:8788`（不要再带 `/v1`） |
| 密钥 | 看板里为该客户端建的 API Key |
| 模型 ID | `claude-sonnet-5`（或 `claude-opus-4-1` / `claude-haiku-4-5-20251001`） |

注意 Messages 协议适配默认关闭，需要 `WB_ENABLE_ANTHROPIC_MESSAGES=1` 或看板里打开。

**codex** → `~/.codex/config.toml`：

```toml
model = "deepseek-v4.1-flash"
model_reasoning_effort = "xhigh"       # 网关会把它变成上游的 max
model_provider = "wblan"

[model_providers.wblan]
name = "WB 路由器直连"
base_url = "http://<网关地址>:8788/v1"
env_key = "WB_API_KEY"
wire_api = "responses"
```

---

## 4. 验收

```bash
GW=http://127.0.0.1:8788
KEY=<API Key>

# A1 claude-sonnet-5 -> 200，回显真实 ID
curl -s $GW/v1/messages -H "Authorization: Bearer $KEY" -H "anthropic-version: 2023-06-01" \
  -d '{"model":"claude-sonnet-5","max_tokens":16,"messages":[{"role":"user","content":"hi"}]}' \
  | jq -r '.model'
# 期望：deepseek-v4.1-flash

# A2 opus 前缀走另一条映射
curl -s $GW/v1/messages -H "Authorization: Bearer $KEY" -H "anthropic-version: 2023-06-01" \
  -d '{"model":"claude-opus-4-1","max_tokens":16,"messages":[{"role":"user","content":"hi"}]}' \
  | jq -r '.model'
# 期望：deepseek-v4-pro

# A3 未知模型仍然 400
curl -s -o /dev/null -w '%{http_code}\n' $GW/v1/messages -H "Authorization: Bearer $KEY" \
  -H "anthropic-version: 2023-06-01" \
  -d '{"model":"gpt-nonexistent","max_tokens":8,"messages":[{"role":"user","content":"hi"}]}'
# 期望：400

# A4 count_tokens 也认 claude 名（Claude 桌面端开会话时会连打多次）
curl -s -o /dev/null -w '%{http_code}\n' $GW/v1/messages/count_tokens \
  -H "Authorization: Bearer $KEY" -H "anthropic-version: 2023-06-01" \
  -d '{"model":"claude-sonnet-5","messages":[{"role":"user","content":"hi"}]}'
# 期望：200

# B1/B2 effort 归一化：日志里应看到 effort_from=xhigh effort_to=max
curl -s $GW/v1/responses -H "Authorization: Bearer $KEY" \
  -d '{"model":"deepseek-v4.1-flash","input":"hi","reasoning":{"effort":"xhigh"},"stream":false}' >/dev/null
curl -s $GW/v1/responses -H "Authorization: Bearer $KEY" \
  -d '{"model":"deepseek-v4.1-flash","input":"hi","reasoning":{"effort":"high"},"stream":false}' >/dev/null
```

`scripts/acceptance_claude_effort.py` 是本仓库自带的一键验收：起一份假上游、
起网关、按上表逐条打、把**上游实际收到的 model / reasoning_effort** 打出来。
不碰真实账号，也不需要网络。

```bash
python scripts/acceptance_claude_effort.py
```

---

## 5. 回滚

| 想恢复旧行为 | 做法 |
|---|---|
| 只关 claude 别名 | `WB_CLAUDE_ALIASES=off`，或 `POST /settings {"claude_aliases_enabled": false}` |
| 只关 effort 归一化 | `WB_EFFORT_ALIASES=off`，或 `POST /settings {"effort_aliases_enabled": false}` |
| 两者都关 | 上面两条一起；请求逐字节回到改动前 |

`model_aliases` / `reasoning_effort_map` 的既有语义没有变化。
