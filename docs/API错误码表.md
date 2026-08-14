# 小辽 M7 API 错误码表

稳定错误码是 Java 合同的一部分；调用方不要依赖响应正文里的中文文案，只按 `error_code` 分支。所有非 200 响应都带 `request_id` 和 `X-Request-ID` 响应头。

| error_code | HTTP | 含义 | Java 建议处理 |
|---|---|---|---|
| AGENT_UNAUTHORIZED | 401 | Token 缺失或无效 | 停止重试，重新获取 Token |
| AGENT_DEBUG_FORBIDDEN | 403 | 普通调用方请求调试数据 | 不展示调试字段，按正常业务继续 |
| AGENT_REQUEST_INVALID | 422 | 请求不符合合同 | 校验请求字段后重发，不要无限重试 |
| AGENT_INVALID_IDEMPOTENCY_KEY | 422 | 幂等键格式非法 | 修正为 1-128 位 `[A-Za-z0-9_.:@-]` 后重发 |
| AGENT_IDEMPOTENCY_CONFLICT | 409 | 同一幂等键复用于不同请求体 | 为新请求生成新键 |
| AGENT_IDEMPOTENCY_IN_PROGRESS | 409 | 相同幂等请求仍在执行 | 稍后使用相同 Idempotency-Key 重试 |
| AGENT_RATE_LIMITED | 429 | 调用过频 | 指数退避后重试，或提示用户稍后再试 |
| AGENT_RUNTIME_STATE_UNAVAILABLE | 503 | Redis 运行时状态不可用 | 不调用模型，退避后使用相同幂等键重试 |
| AGENT_MODEL_TIMEOUT | 504 | 模型超时 | 可携带相同 Idempotency-Key 重试一次 |
| AGENT_MODEL_UNAVAILABLE | 502 | 模型或 Agent 依赖不可用 | 稍后重试，或走降级文案 |
| AGENT_MODEL_NETWORK | 502 | 模型网络不可用 | 稍后重试 |
| AGENT_MODEL_RATE_LIMITED | 502 | 模型侧限流 | 退避后重试 |
| AGENT_MODEL_HTTP | 502 | 模型 HTTP 错误 | 记录 request_id，稍后重试 |
| AGENT_MODEL_INVALID_RESPONSE | 502 | 模型返回不符合合同 | 记录 request_id，按系统故障处理 |
| AGENT_MODEL_ERROR | 502 | 模型返回错误 | 记录 request_id，稍后重试 |
| AGENT_CONFIG_MISSING | 启动 | 缺少 Key/模型配置 | 不进入用户链路，服务拒绝启动 |
| AGENT_INVALID_JSON | 502 | 模型结构化输出失败 | 记录 request_id，按系统故障处理 |
| AGENT_INSPECTION_FAILED | 502 | 副 Agent 质检连续失败 | 按保守降级处理，不展示候选回复 |
| AGENT_SAFETY_BLOCKED | 200 | 安全边界拦截 | 正常业务响应，直接展示固定话术 |
| AGENT_CRISIS_BLOCKED | 200 | 危机信号拦截 | 正常业务响应，直接展示固定危机话术 |
| AGENT_KB_UNAVAILABLE | 502 | 知识库不可用 | 按系统故障处理，稍后重试 |
| AGENT_TTS_DISABLED | 501 | 语音合成未开启 | 隐藏语音入口，或提示语音功能稍后开放 |
| AGENT_SESSION_NOT_FOUND | 预留 | 会话不存在 | 当前 /v1/chat 不校验会话存在；预留由 Java 重传授权摘要 |

安全和危机拦截是正常业务响应，不是 HTTP 系统故障。模型超时、不可用、网络和结构化输出失败必须通过 502/504 暴露，不能用 200 隐藏。
