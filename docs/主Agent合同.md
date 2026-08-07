# 主 Agent 合同

## 输入

- `user_text`：最多 2000 字符。
- `history`：仅保留最近 6 条 user/assistant，总计最多 4000 字符；system 历史丢弃。
- `memory_context`：默认空，最多 2000 字符，只允许第 6 步授权过滤后的摘要。
- RAG context：最多 6000 字符。
- 用户、历史、记忆和 RAG 均作为不可信数据分隔，不进入 system 内容。

## 主输出

主模型必须返回完整 JSON：`reply`、`intent`、`action`、`risk_hint`。缺字段、额外字段、错误类型、纯文本或空回复均为 `AGENT_INVALID_JSON`，不会把供应商原文发送给用户。

`risk_hint` 只允许 `none`、`possible_crisis`、`medical_boundary`。

## Action 白名单

| module | page | params |
|---|---|---|
| M1 | `/pages/checkin/index` | `{}` |
| M2 | `/pages/games/index` | `{}` |
| M3 | `/pages/exercise/index` | `{}` |
| M5 | `/pages/community/index` | `{}` |

Action 还必须包含非空 `reason` 和 ISO 8601 `expires_at`。当前页面白名单为待 Java 确认的原型版本，不代表生产路径已冻结。

非法 module、page、URL、额外字段或 params 会被清空，并记录 `AGENT_INVALID_ACTION`。assessment 可以是 intent，但不能成为越权 action。

## 错误分类

- `AGENT_MODEL_TIMEOUT`
- `AGENT_MODEL_NETWORK`
- `AGENT_MODEL_HTTP`
- `AGENT_MODEL_RATE_LIMITED`
- `AGENT_MODEL_INVALID_RESPONSE`
- `AGENT_INVALID_JSON`

错误回复使用与 `PROMPT_VERSION` 对应的版本化降级话术。内部结果保留 request_id、主模型名和 Prompt 版本，不向用户泄露 Key、供应商错误体或原始无效回复。
