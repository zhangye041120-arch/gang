# M7 Python 智能体安全与上线差距报告

## 执行摘要

当前仓库在双 Agent 安全审核、危机确定性预检、严格 Pydantic 合同和原文不落库方面已有较好基础。当前没有发现代码可直接定性的 Critical 远程代码执行或 SQL 注入，但存在 4 个 High 级上线阻断问题和多个 Medium 级生产基线缺口。

最优先的风险是生产依赖失败后静默转内存导致数据丢失，以及用户级资源只受一枚共享 Token 保护。如果 API 始终由已验证的 Java/企微网关通过隔离内网调用，第二项的可利用性会降低，但仍应实施网关签名、用户绑定和防重放。

## High

### SEC-001 生产数据库失败时静默回退内存

- **Rule ID:** FASTAPI-DEPLOY-FAIL-CLOSED / DATA-DURABILITY-001
- **Severity:** High
- **Location:** `xiaoliao_agent/agent.py:485-493`
- **Evidence:** 数据库不可达时只告警，然后将 `knowledge_database_url` 置空；后续用户记忆、行动、危机和质量日志全部改用内存仓库。
- **Impact:** 服务表面正常接受请求，但数据在重启或切 worker 后丢失；危机记录与授权证据也可丢失。
- **Fix:** 增加生产模式；生产必须配置并连通 PostgreSQL，schema 版本不符或连接失败时拒绝 readiness/启动。
- **Mitigation:** 实施前仅限本地或受限试验，禁止宣称持久化可用。
- **False positive notes:** 外部进程守护如果会检测该 warning 并立即停机，可减轻影响；本仓库未见该保护。

### SEC-002 共享服务 Token 没有用户级对象授权

- **Rule ID:** FASTAPI-AUTHZ-001
- **Severity:** High
- **Location:** `API服务.py:309-318`, `API服务.py:665-690`
- **Evidence:** `authorize()` 只验证全局 API/debug Token；`/v1/me/summary` 从 query 取任意 `user_id`，隐私删除从 body 取任意 `user_id`，没有将调用方与用户绑定。
- **Impact:** 一旦服务 Token 泄露或内网调用方被滥用，攻击者可查看或删除其他用户数据。
- **Fix:** API 仅限内网；在 Bearer 服务身份外增加带 `user_id`、body 指纹、时间戳和 nonce 的网关 HMAC 签名；对比签名主体与资源用户。
- **Mitigation:** 实施前使用网络 ACL/mTLS 限制为单一网关，并缩短 Token 轮换周期。
- **False positive notes:** 若 Java 网关已严格绑定用户且 Python API 在无法横向访问的隔离网络中，实际可利用性较低；本仓库无运行时证据。

### SEC-003 隐私删除不完整，且删除审计会被删掉

- **Rule ID:** PRIVACY-DELETE-001
- **Severity:** High
- **Location:** `API服务.py:675-690`, `xiaoliao_agent/user_data.py:255-258`, `xiaoliao_agent/user_data.py:359-363`
- **Evidence:** API 先写 `privacy.delete` 审计再调用 `delete_user`；PostgreSQL 仓库随后删除该用户的全部 `ai_audit_logs`。当前删除只编排用户/授权/会话事件和长期记忆，未覆盖行动、危机、提醒、质量日志与 Redis 缓存。
- **Impact:** 用户收到“已删除”但关联数据仍可留存；同时无法证明删除何时、删了什么。
- **Fix:** 使用专用删除编排服务和带计数的数据库事务；清理全部用户表与 Redis key；只保留无原始 user ID 的 HMAC 删除审计。
- **Mitigation:** 暂停对外宣称“彻底删除”，用人工数据库核查补偿。
- **False positive notes:** 如果外部系统还有统一删除编排，需提供表级覆盖和执行证据；本仓库未见。

### SEC-004 生产提醒仍使用内存仓库

- **Rule ID:** DATA-DURABILITY-002
- **Severity:** High
- **Location:** `xiaoliao_agent/agent.py:564`
- **Evidence:** `ReminderService` 无条件使用 `MemoryReminderRepository`，即使已配置 PostgreSQL。
- **Impact:** 进程重启、worker 切换或滚动发布后，用户对话中设置的提醒丢失。
- **Fix:** 增加提醒迁移和 PostgreSQL 仓库；生产只允许持久仓库。
- **Mitigation:** 实施前将话术中的“已记录提醒”明确限定为当前进程，或临时关闭提醒意图。
- **False positive notes:** 如果提醒不对用户开放，风险不可利用；PRD 和 README 都将其列为用户能力。

## Medium

### SEC-005 授权不是 API 层唯一真值

- **Rule ID:** PRIVACY-CONSENT-001
- **Severity:** Medium
- **Location:** `API服务.py:458-463`, `API服务.py:547-551`
- **Evidence:** `/v1/chat` 和流式端点直接将请求体中的 `user_summary` 和 `context.consent.personalization` 传给 Agent，没有先读取服务端授权。
- **Impact:** 网关错误或被滥用时，未授权的用户摘要可被发给模型；授权执行不一致。
- **Fix:** 数据库授权是唯一真值；请求只能申请使用已授权能力，不能提升权限。未授权时丢弃 summary。
- **Mitigation:** Java 网关只从受信授权库填充该字段，不接受客户端自报。
- **False positive notes:** 记忆仓库内部会再次检查授权，能降低持久化风险，但不阻止 `user_summary` 进入本轮模型上下文。

### SEC-006 限流与幂等只在单进程内有效

- **Rule ID:** AVAILABILITY-STATE-001
- **Severity:** Medium
- **Location:** `API服务.py:113-172`, `API服务.py:237-253`
- **Evidence:** 限流使用本地 dict/list，幂等使用本地 dict 和 `asyncio.Condition`，且结果没有 TTL 删除。
- **Impact:** 多 worker 可绕过限流或重复调用模型/产生副作用；长期运行的幂等 key 无界增长。
- **Fix:** 使用 Redis 原子脚本/事务实现共享限流、执行锁和幂等结果 TTL。
- **Mitigation:** 仅运行一个 worker，在网关做额外限流与幂等。
- **False positive notes:** 单进程本地试点下不存在跨 worker 不一致，但仍有无界 key 问题。

### SEC-007 FastAPI 生产边界未固化

- **Rule ID:** FASTAPI-OPENAPI-001 / FASTAPI-HOST-001 / FASTAPI-REQSIZE-001
- **Severity:** Medium
- **Location:** `API服务.py:292-298`, `API服务.py:367-375`
- **Evidence:** FastAPI 使用默认 docs/openapi 路径；没有 TrustedHost 和请求体上限；`/health` 公开返回模型 ID 和知识块数量。
- **Impact:** 增加内部合同暴露和 Host/request-body DoS 攻击面；健康接口可泄露部署细节。
- **Fix:** 生产禁用 docs/openapi，启用 Host 白名单和请求体限制，拆分最小 live/ready 响应。
- **Mitigation:** 在反向代理阻断 docs/openapi，校验 Host 并限制 body。
- **False positive notes:** 外部 Nginx/网关可能已实施这些控制，但本仓库没有可验证配置。

### SEC-008 会话引用使用无密钥普通哈希

- **Rule ID:** PRIVACY-PSEUDONYM-001
- **Severity:** Medium
- **Location:** `xiaoliao_agent/user_data.py:365-369`, `xiaoliao_agent/config.py:82,177`
- **Evidence:** 消息引用是直接 `sha256(content)`；质量 user hash 默认盐为仓库可见的 `xiaoliao-local-quality`。
- **Impact:** 老年用户常见短句可被字典猜测验证；默认盐无法提供部署隔离。
- **Fix:** 生产要求独立高强度隐私 HMAC 密钥，对内容和用户引用使用 HMAC-SHA256；拒绝默认盐。
- **Mitigation:** 限制该表的数据库权限与导出，不向调试 API 返回内容引用。
- **False positive notes:** 引用不是原文，不存在直接明文泄露；风险主要来自可猜测短句。

### SEC-009 依赖和生产运行包未锁定

- **Rule ID:** FASTAPI-SUPPLY-001 / FASTAPI-DEPLOY-001
- **Severity:** Medium
- **Location:** `requirements.txt:1-8`, `docker-compose.yml:1-22`, `启动API.py:12`
- **Evidence:** 所有 Python 依赖只设最低版本，运行和测试依赖混合；Compose 只包含数据库；没有应用 Dockerfile/迁移服务/非 root 验证；生产进程模型没有被固化。
- **Impact:** 相同代码在不同时间可安装不同依赖；无法生成可重复、可扫描、可回滚的发布制品。
- **Fix:** 拆分并精确锁定依赖，增加非 root Dockerfile、Redis/migrate/API Compose 服务和 CI 依赖/镜像扫描。
- **Mitigation:** 手工导出已验证环境的精确版本并保留安装清单。
- **False positive notes:** 外部发布平台可能会另行生成锁文件和镜像；本仓库没有证据。

### SEC-010 健康检查可在持久化降级时报告正常

- **Rule ID:** AVAILABILITY-READINESS-001
- **Severity:** Medium
- **Location:** `API服务.py:367-375`, `xiaoliao_agent/agent.py:485-493`
- **Evidence:** `/health` 无条件返回 `status=ok`；Agent 可能已经回退内存库。
- **Impact:** 调度器继续将流量发往无法持久化的实例，监控也无法发现数据风险。
- **Fix:** 拆分 liveness/readiness；readiness 验证 PostgreSQL、Redis、schema、关键配置和知识库。
- **Mitigation:** 在外部探针增加数据库/Redis 专项检查。
- **False positive notes:** 如果当前 `/health` 仅作 liveness 且外部已有 readiness，风险可降级；本仓库文档把它作为主要健康证据。

## 发布 Gate（非代码漏洞）

### REL-001 外部上线证据尚未完成

- **Severity:** Release blocker
- **Location:** `docs/运营流程与上线清单.md` 的“小辽 M7 上线前置动作清单”
- **Location:** `docs/运营流程与上线清单.md` 的“小辽 M7 上线前置动作清单”
- **Evidence:** 真实 7 天灰度、企微消息闭环、危机转介/演练、监控阈值、Java action 冻结和发布包安全都被列为待执行动作。
- **Impact:** 即使代码完成，仍不能宣称已正式上线。
- **Fix:** 完成代码上线候选后，由 Java/企微、安全、运营和隐私负责人提供真实证据并签字。

## 修复顺序

1. SEC-001、SEC-004、SEC-006、SEC-010：生产 fail-closed、PostgreSQL/Redis、持久提醒和 readiness。
2. SEC-002、SEC-005：网关签名、用户绑定和服务端授权真值。
3. SEC-003、SEC-008：全量隐私删除与 HMAC 引用。
4. SEC-007、SEC-009：FastAPI 生产基线、依赖、容器、CI 与扫描。
5. REL-001：完成所有真实外部 Gate。
