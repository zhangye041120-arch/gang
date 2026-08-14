# M7 Python 智能体可部署上线候选设计

## 1. 范围与结论

本设计只完善当前仓库中的 M7 Python 智能体服务。对话主模型保持 `deepseek-v4-flash`，副模型保持 `qwen3.7-flash-2026-07-15`，不替换模型，不改变双 Agent 安全架构。

交付目标是“可部署上线候选”，即代码、数据、安全、容器、监控和自动验证达到可进入真实灰度的标准。以下外部事项必须保持发布 Gate，不能通过代码伪造完成：

- Java/企微网关的真实身份校验、签名和消息收发联调。
- 危机热线、值班人、误报流程和真实演练。
- M1/M2/M3/M5 页面路径与 action 合同由 Java/小程序负责人冻结。
- 10-20 位授权用户的 7 天灰度和发布负责人签字。

## 2. 已有基线与核心问题

已有基线包括：双 Agent 生成/质检/一次重写，确定性危机与安全预检，CBT 与教训 RAG，用户记忆骨架，行动建议，授权/摘要/删除 API，质量日志，企微发送适配器，天气快路径，以及 376 条自动化测试。

上线候选阻断项是：

1. 生产数据库不可达时会静默切到内存仓库，重启后丢数据。
2. 限流与幂等是单进程内存状态，无 TTL 清理，不支持多 worker。
3. 对话提醒仍使用内存仓库。
4. 服务级 Token 没有绑定终端用户，用户级资源缺少网关签名与对象授权。
5. 请求体中的个性化开关和摘要可直接进入 Agent，数据库授权不是 API 层的唯一真值。
6. 隐私删除未覆盖行动、危机、提醒、质量与 Redis 状态，且现有删除审计会被同步删掉。
7. 生产文档端点、Host 校验、请求体限制、依赖锁定、应用镜像、迁移编排和真实监控均未完成。

## 3. 目标架构

```text
个人微信 / 小程序
        |
        v
Java / 企微网关（公网身份与 user_id 绑定）
        |
        | 内网 + Bearer + HMAC 签名
        v
M7 FastAPI（非 root，多 worker）
  |       |          |
  |       |          +--> DeepSeek / Qwen（现有模型）
  |       +--> Redis 7（限流/幂等/nonce/短缓存）
  +--> PostgreSQL + pgvector（持久数据/记忆/RAG/审计）
```

依赖原则：生产模式下 PostgreSQL 和 Redis 都是必需依赖。任何持久化依赖不可用时 fail closed，不得自动切换为内存实现。开发与测试模式继续支持内存仓库。

## 4. 生产配置与启动校验

新增 `APP_ENV=development|test|production`。`production` 启动时执行统一 `validate_production()`，至少校验：

- DeepSeek/Qwen Key 与现有模型 ID。
- 长度足够的 API Token、网关 HMAC 密钥和隐私 HMAC 密钥；密钥不得相同。调试能力生产默认关闭；只有显式开启时才要求独立调试 Token 与内网白名单。
- `KNOWLEDGE_DATABASE_URL` 与 `REDIS_URL`，并执行连通性校验。
- 数据库 schema 已达到代码要求的最新版本。
- `QUALITY_HASH_SALT` 不得使用仓库默认值。
- 受信 Host 列表、网关时钟偏差、请求体上限、Redis TTL 和备份保留天数在允许范围内。
- `API_TEST_MODE=true` 在生产模式下直接拒绝启动。
- 危机 route 或通知发送器未配置时 readiness 失败，但本地开发仍可用明确告警运行。

## 5. PostgreSQL 迁移与持久化

### 5.1 迁移执行器

保留现有 SQL 迁移文件，增加一个小型顺序迁移执行器：

- 创建 `schema_migrations(version, checksum, applied_at)`。
- 按文件名版本顺序执行，使用 PostgreSQL advisory lock 防止并发迁移。
- 已执行版本的 checksum 改变时立即失败，禁止篡改历史迁移。
- API 进程只校验 schema；独立 `migrate` 进程负责应用迁移。

### 5.2 持久化完整性

- 用 PostgreSQL 仓库替换生产对话提醒的 `MemoryReminderRepository`。
- 用 Redis 替换生产的内存限流与幂等协调器。
- 幂等数据包含 principal、幂等键、请求指纹、状态码和结果，TTL 默认 24 小时。
- 限流同时约束服务 principal 和签名用户，所有 worker 共享计数。
- 短期缓存仅保存可重建数据，不把 Redis 当作用户记忆的唯一存储。

## 6. 长期记忆与行动回流

### 6.1 记忆类型与授权

保留已有记忆类型：`profile`、`family_relationship`、`interest_preference`、`key_event`、`emotion_trend`、`action_summary`、`conceptualization_clue`。

- `personalization=true` 是任何长期记忆读写的前提。
- `emotion_trend`、心理练习摘要和概念化线索还需要 `sensitive=true`。
- API 不信任请求体自报的授权，每轮以 PostgreSQL 中的授权为唯一真值。
- 用户未授权时，忽略 `user_summary`，不检索、不提取、不缓存个人记忆。

### 6.2 记忆模型

在现有 `ai_memories` 上增加：

- `memory_key`：如 `profile.name`、`profile.age`、`family.grandson.exam`，用于识别同一事实。
- `supersedes_memory_id`：新事实替换哪条旧事实。
- `source_type`：`conversation|action_event|manual_correction`。

对年龄、姓名、常住城市等单值事实，每个用户和 `memory_key` 只允许一条有效记忆。冲突时将旧记忆设为失效并记录替代链，不直接覆盖审计历史。

### 6.3 检索与 Prompt 注入

- 第一版使用“类型优先级 + 关键词相关性 + 新鲜度”混合排序，不强制新增对话模型。
- 现有 pgvector 路径在配置独立 embedding 能力时作为语义增强；未配置时必须保持可预测的非向量检索。
- 默认最多返回 6 条、2000 字，每条附类型和时间，不向模型暴露数据库 ID。
- 修正、删除、授权变更或行动回流后，立即清理该用户的 Redis 记忆缓存。

### 6.4 用户可控 API

在现有 `/v1/me/summary` 之外增加内网用户级端点：

- `GET /v1/me/memories`：分页查看有效记忆。
- `PATCH /v1/me/memories/{memory_id}`：用户明确修正，新值标记为 `manual_correction`。
- `DELETE /v1/me/memories/{memory_id}`：删除单条记忆。
- `POST /v1/privacy/delete-request`：删除全部个人数据并返回删除范围。

以上端点的签名用户必须与资源 `user_id` 一致。

### 6.5 M1/M2/M3/M5 行动回流

新增 `POST /v1/action-events`，请求包含：

- `event_id`：Java/小程序产生的全局唯一 ID，用于幂等。
- `recommendation_id`、`user_id`、`module`、`event_type`、`occurred_at`。
- 受限 `summary` 和模块专用的白名单 `metadata`。

`event_type` 只允许 `accepted|completed|declined|expired`。同一 `event_id` 和相同请求重放返回已有结果；请求不同时返回冲突。

数据最小化规则：

- M2 可保存训练类型、难度、完成时间和结果摘要。
- M1 情绪与 M3 练习仅在敏感授权后保存最小摘要，默认不保存完整原文。
- M5 仅保存行动类型与成就摘要，不保存帖子、评论或私聊正文。
- 完成事件可生成 `action_summary` 记忆；拒绝事件只用于调整引导频率，不生成负面人格标签。

## 7. 网关身份、授权与防重放

M7 API 只接受 Java/企微网关的内网请求。生产用户级请求同时需要：

1. `Authorization: Bearer <service-token>` 识别网关服务。
2. `X-Gateway-User-Id`、`X-Gateway-Timestamp`、`X-Gateway-Nonce`、`X-Gateway-Signature`。
3. 签名串固定为 `timestamp\nnonce\nmethod\npath\nuser_id\nsha256(body)`，使用 HMAC-SHA256。
4. 时间偏差默认不超过 300 秒；nonce 通过 Redis `SET NX EX` 使用一次。
5. 签名 `user_id` 必须与 body/query 中的用户一致。

调试与管理能力使用独立 Token：

- 普通网关 Token 不能请求 debug 字段或知识库管理端点。
- 调试 Token 只允许受限调试，不自动获得数据删除或知识库管理权限。
- 管理端点使用单独管理 Token 与网络白名单。

## 8. 隐私删除与内容引用

隐私删除由单一 `PrivacyDeletionService` 编排，覆盖：

- 用户、授权、会话引用、长期记忆及向量。
- 行动建议、行动事件、对话提醒。
- 可识别用户的危机记录、质量日志与所有 Redis key。

删除在数据库事务中完成可同库数据。Redis 清理失败时将任务标记为待重试，readiness/告警不得掩盖失败。

删除后只保留一条无法还原身份的删除审计：`subject_hmac`、request ID、删除范围、结果和时间。不保留原始 `user_id`。危机事件如果因法定义务必须保留，只可保留经隐私负责人确认的去识别最小安全统计。

会话和候选回复引用从普通 SHA-256 改为部署密钥 HMAC-SHA256，避免常见短句被字典反推。日志不记录原始消息、Token、签名、nonce 或敏感记忆。

## 9. FastAPI 生产安全基线

- 生产关闭 `/docs`、`/redoc` 和公开 `/openapi.json`。
- 使用 `TrustedHostMiddleware` 并要求明确 Host 白名单。
- 默认不开 CORS；M7 不接受浏览器直连。
- 应用层限制请求体，默认 64 KiB；网关层设置更早的对应限制。
- 对所有响应增加 `X-Request-ID`、`X-Content-Type-Options: nosniff`、`Cache-Control: no-store`（健康检查除外）。
- 生产不启用 debug traceback 或 reload。
- 只信任明确的反向代理 IP，不无条件信任 `X-Forwarded-*`。
- `/health/live` 只返回进程存活；`/health/ready` 校验配置、schema、PostgreSQL、Redis、知识库和危机 route，不返回模型名或数据量。

## 10. 容器与运行进程

交付：

- 生产 `Dockerfile`：固定 Python 版本，多阶段安装，非 root 用户，只复制运行必要文件。
- `.dockerignore`：排除 `.env*`、`.git`、测试/评估输出、日志、缓存和本地数据。
- 拆分并精确锁定运行依赖与开发依赖。
- `docker-compose` 包含 `postgres`、`redis`、`migrate`、`agent-api`，应用不默认发布到公网。
- Uvicorn 稳定多 worker 运行，支持优雅停机；worker 数与 CPU/模型并发配置化。
- 应用停止时关闭 HTTP 连接池与 Redis 连接。

## 11. 可观测性

日志使用 JSON，固定字段包含 timestamp、level、service、environment、request ID、route、status、latency_ms 和 error_code。严禁将 user ID、消息正文、记忆内容作为 Prometheus label。

Prometheus 指标至少包含：

- HTTP 请求量、延迟、状态码、429 和 5xx。
- Agent 总延迟与 RAG/主模型/副模型/重写阶段延迟。
- 模型错误码、幂等命中、Redis/PostgreSQL 不可用。
- 记忆写入/检索/修正/删除计数与缓存命中，仅使用记忆类型等低基数标签。
- 危机拦截、转介失败和通知重试计数，不包含用户证据。

告警阈值取消“待确认”占位。候选 SLO：试点可用率 `>=99.5%`，5xx `<1%`，天气 p95 `<=3s`，普通对话 p50 `<=8s` / p95 `<=20s`，固定危机评估硬漏放 `=0`。

## 12. CI、备份与发布 Gate

CI 必须执行：

1. 格式/静态检查与全量 `pytest`。
2. PostgreSQL + Redis 集成测试，包含从空库迁移、重复迁移和 checksum 篡改拦截。
3. 网关签名、越权、防重放、授权、记忆和隐私删除测试。
4. API/OpenAPI 合同测试，以及 55 条离线安全评估与 RAG 评估。
5. 依赖漏洞扫描、密钥扫描、容器构建、非 root 与健康检查验证。

备份脚本每日对 PostgreSQL 生成加密备份，保留周期配置化，不将备份放入代码仓库。上线前必须在独立数据库完成一次恢复演练。

发布使用不可变镜像标签，同时冻结代码、Prompt、知识库和 schema 版本。数据库迁移只向前；应用回滚只允许回到与当前 schema 兼容的镜像。

## 13. 实施批次

### 批次 A：生产运行和持久化

- `APP_ENV`、生产配置校验、fail-closed PostgreSQL/Redis。
- 迁移执行器、schema 校验、持久提醒。
- Redis 限流、幂等与 nonce 基础能力。
- live/readiness 健康检查。

### 批次 B：网关安全、记忆和隐私

- HMAC 网关签名、用户绑定、权限 Token 分离。
- 服务端授权真值、记忆键/冲突版本、查看/修正/删除。
- M1/M2/M3/M5 行动事件回流。
- 完整隐私删除与 HMAC 内容引用。

### 批次 C：交付和可观测性

- FastAPI 生产中间件、Docker/非 root/多 worker。
- JSON 日志、Prometheus 指标和已确认告警阈值。
- CI、密钥/依赖扫描、备份/恢复和容器冒烟测试。
- 更新 README、OpenAPI、Java 签名示例和发布 Gate 文档。

每个批次独立走 TDD、全量回归和代码审查。批次 C 通过后只能标记“可部署上线候选”；外部 Gate 全部关闭后才能标记“已正式上线”。

## 14. 验收标准

- 生产缺 PostgreSQL、Redis、签名密钥、隐私密钥或 schema 时进程拒绝就绪。
- 多 worker 下限流、幂等和 nonce 防重放共享一致。
- 重启 API 后提醒、用户记忆和行动事件仍存在。
- 无授权时记忆不读、不写、不入模型；敏感信息没有敏感授权时不持久化。
- 签名用户 A 不能查看、修改或删除用户 B 的数据。
- 重放、超时签名、篡改 body 和不一致 user ID 全部被拒绝。
- 隐私删除后所有可识别个人数据与缓存不可检索，仅剩不可逆删除审计。
- 空库迁移、重放迁移、备份恢复、容器启动和 readiness 全部有自动或可重复证据。
- 全量测试、离线安全评估、RAG 评估、依赖/密钥扫描和镜像检查全部通过。
