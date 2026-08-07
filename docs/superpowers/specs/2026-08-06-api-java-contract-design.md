# 小辽 M7 第 9 步：API 与 Java 合同设计

## 目标

冻结并补齐版本化的 `POST /v1/chat` 外部合同，使 Java 调用方不依赖 Prompt、RAG、DeepSeek 或 Qwen 的内部实现。离线验收覆盖严格 Schema、鉴权、限流、请求追踪、错误码、并发隔离和重试幂等；真实 Java 调用属于外部资源验收，不用 Mock 冒充完成。

## 已冻结决策

- 外部 `intent` 仅允许 `chat`、`checkin`、`game`、`exercise`、`assessment`、`community`。
- 内部 `emotion_support` 映射为外部 `chat`；行动模块优先映射为 `M1 -> checkin`、`M2 -> game`、`M3 -> exercise`、`M5 -> community`。
- 生产鉴权使用 `Authorization: Bearer <API_TOKEN>`。
- 生产端口由唯一配置 `API_PORT` 提供，默认值冻结为 `8081`。
- 旧 `POST /chat` 仅作为有期限的兼容入口，标记弃用并复用同一 Agent 能力，不建立第二套业务逻辑。

## 合同模型

`POST /v1/chat` 请求严格包含：

- `user_id`：1 至 64 个允许字符。
- `session_id`：1 至 128 个允许字符。
- `message`：1 至 2000 个字符。
- `context`：只允许 `consent.personalization` 和 `user_summary`。
- `debug`：严格布尔值，默认 `false`。

请求及嵌套对象禁止额外字段，因此 `system_prompt`、任意扩展指令和 `system` 历史不能进入 Agent。

正常响应固定包含 `session_id`、`reply`、`intent`、`action`、`blocked`、`crisis_detected`、`safety_violation`、`rewritten`。调试来源和 inspection 只在 `debug=true` 且调用方持有配置的调试权限时返回；默认响应不暴露内部 Prompt、模型原文或检索正文。

## 请求处理流程

1. 接受符合字符约束的可信网关 `X-Request-ID`，否则由服务端生成 UUID；请求 ID 不包含用户隐私。
2. 校验 Bearer Token，并以 Token 摘要作为受信主体标识。
3. 校验调试权限。普通调用方请求 `debug=true` 时返回权限错误。
4. 按受信主体和 `user_id` 两个维度组合限流。
5. 按“受信主体 + Idempotency-Key”协调幂等；请求指纹必须一致。
6. 不读取 `/v1/chat` 的进程内会话历史，只把 Java 授权的 `user_summary`、同意状态、`user_id` 和 `session_id` 传入 Agent。
7. 将 Agent 内部意图映射为冻结的 6 值合同，并用严格响应模型输出。

## 幂等与并发

首个幂等请求成为执行者；同一进程中的并发重复请求等待首个结果，不重复调用 Agent。完成结果按受信主体和幂等键缓存。相同键、不同规范化请求体返回 `409 AGENT_IDEMPOTENCY_CONFLICT`。

此实现保证单服务进程内的并发重试不重复触发危机事件、行动推荐或质量日志。跨进程和重启后的持久幂等需要共享存储或网关能力，留作部署增强项，不在尚未确定生产拓扑时引入新数据库表。

## 错误处理

- `401 AGENT_UNAUTHORIZED`：Token 缺失或无效。
- `403 AGENT_DEBUG_FORBIDDEN`：普通主体请求调试数据。
- `409 AGENT_IDEMPOTENCY_CONFLICT`：幂等键复用于不同请求。
- `422 AGENT_REQUEST_INVALID`：请求合同不合法；特殊非法幂等键可使用稳定细分码。
- `429 AGENT_RATE_LIMITED`：受信主体或用户维度超限。
- `502 AGENT_MODEL_UNAVAILABLE`：模型或 Agent 依赖不可用。
- `504 AGENT_MODEL_TIMEOUT`：模型调用超时。

所有业务错误包含 `error_code` 和 `request_id`，响应头统一带 `X-Request-ID`。安全和危机拦截是成功执行后的正常业务响应，不伪装成系统故障。

## OpenAPI 与兼容

OpenAPI 明确声明 HTTP Bearer 安全方案、严格请求/响应 Schema、6 值 `intent`、错误响应和弃用的 `/chat`。旧接口给出移除条件：Java 和企微调用全部迁移到 `/v1/chat` 且完成一个发布周期验证后移除；真实迁移状态未验证前不写固定完成日期。

交付物包括生成的 `openapi.json`、错误码表、curl 示例和可编译的 Java 11+ `HttpClient` 调用示例。Java 示例只证明合同代码可构建和指向测试服务；未取得 Java 运行环境或真实服务调用证据时明确写“待外部资源”。

## 测试与验收

测试先行覆盖：合法请求、缺字段、额外字段、超长文本、非法 ID、`system_prompt` 注入、UTF-8、鉴权、调试权限、限流、可信与无效请求 ID、模型超时、模型不可用、并发会话隔离、串行及并发重复请求、幂等冲突、外部意图映射和 OpenAPI 字段锁定。

先运行第 9 步目标测试，再运行 `python -m pytest -q` 全量回归。完成后更新 README、`.env.example`、启动脚本及 `M7_PROGRESS.md`；真实 Java 联调继续标为外部阻塞。

## 修改边界

允许修改 API 合同、配置、相关测试、生成文档、Java 示例、README、启动入口和进度记录。除传递稳定请求标识所必需的最小签名调整外，不改变主 Agent、Inspector、RAG、安全、记忆和行动业务规则。
