# Agent 合同

本文合并主 Agent、副 Agent、安全与危机、行动闭环、长期记忆、质量日志六份合同。

## 主 Agent 合同

### 输入

- `user_text`：最多 2000 字符。
- `history`：仅保留最近 6 条 user/assistant，总计最多 4000 字符；system 历史丢弃。
- `memory_context`：默认空，最多 2000 字符，只允许授权过滤后的摘要。
- RAG context：最多 6000 字符。
- 用户、历史、记忆和 RAG 均作为不可信数据分隔，不进入 system 内容。

### 主输出

主模型必须返回完整 JSON：`reply`、`intent`、`action`、`risk_hint`。缺字段、额外字段、错误类型、纯文本或空回复均为 `AGENT_INVALID_JSON`，不会把供应商原文发送给用户。

`risk_hint` 只允许 `none`、`possible_crisis`、`medical_boundary`。

### Action 白名单

| module | page | params |
|---|---|---|
| M1 | `/pages/checkin/index` | `{}` |
| M2 | `/pages/games/index` | `{}` |
| M3 | `/pages/exercise/index` | `{}` |
| M5 | `/pages/community/index` | `{}` |

Action 还必须包含非空 `reason` 和 ISO 8601 `expires_at`。当前页面白名单为待 Java 确认的原型版本，不代表生产路径已冻结。

非法 module、page、URL、额外字段或 params 会被清空，并记录 `AGENT_INVALID_ACTION`。assessment 可以是 intent，但不能成为越权 action。

### 错误分类

- `AGENT_MODEL_TIMEOUT`
- `AGENT_MODEL_NETWORK`
- `AGENT_MODEL_HTTP`
- `AGENT_MODEL_RATE_LIMITED`
- `AGENT_MODEL_INVALID_RESPONSE`
- `AGENT_INVALID_JSON`

错误回复使用与 `PROMPT_VERSION` 对应的版本化降级话术。内部结果保留 request_id、主模型名和 Prompt 版本，不向用户泄露 Key、供应商错误体或原始无效回复。

## 副 Agent 合同

### 输入

- `user_text`、`intent`、候选回复，以及与主 Agent 相同的 RAG context（最多 6000 字符）。
- RAG 作为不可信参考资料，用于判断候选回复是否遵循知识库，不作为系统指令；RAG 为空时仍按通用安全陪伴原则检查。

### 严格输出

Inspector 必须返回固定九字段 JSON：`crisis_detected`、`safety_violation`、`intent_accurate`、`age_appropriate`、`cbt_appropriate`、`issues`、`suggestion`、`error_pattern`、`lesson`。缺字段、额外字段、错误类型和字符串布尔均视为无效响应。

`error_pattern` 维度固定为：`none`、`crisis`、`medical_boundary`、`unsafe_content`、`intent_mismatch`、`age_inappropriate`、`cbt_inappropriate`、`invalid_response`、`unknown`。模型自造字符串归入 `unknown`，非法类型归入不安全失败。

### 处理终态

- 危机或医疗硬失败：立即使用固定兜底，不发送候选回复。
- Inspector 解析、超时或合同失败：按不安全处理，不发送候选回复。
- 首次软失败：受限 suggestion 只进入一次重写。
- 重写后硬失败：固定兜底。
- 重写后二次软失败：返回版本化保守降级，记录 `AGENT_INSPECTION_FAILED`，不发送未通过回复，不做第三次重写。

### Lesson 审核

Inspector 的非空 lesson 只写入内存待审候选，初始状态为 `pending`。人工可改为 `approved` 或 `rejected`，必须记录 reviewer 和 reason。未审批 lesson 不改变 Prompt；持久化完成前不宣称已持久化。

## 安全与危机合同

### 风险类别

风险固定为 `crisis`、`medical_boundary`、`unsafe_content`、`normal`。规则只提供技术预警，不是临床诊断。事件仅保存规则 ID 等最小证据码，不保存完整敏感原文。

### 执行顺序

输入确定性预检先于 RAG、记忆和模型。候选回复发送前再次做输出预检。危机、医疗和注入输入均不调用普通生成链路。

### 固定回复

- 危机话术版本：`crisis-v1.0.0`
- 医疗边界版本：`medical-v1.0.0`
- 越权输入版本：`unsafe-v1.0.0`

危机话术确认即时安全、建议不要独处，并建议联系可信任的人、人工支持或当地资源。未配置真实 route 时只记录 `route_unconfigured`，不编造热线号码。

### 事件与通知

`ai_crisis_events` 仅供高权限审计，迁移撤销 PUBLIC 权限。默认保留期配置为 365 天；实际清理任务需产品、合规和数据库负责人审批后建立。

通知 route 从 `.env` 读取。通知失败按配置次数重试，同一 event_id 只处理一次；失败记录结构化告警，但不影响固定安全回复。

### 当前上线阻塞

`CRISIS_ROUTE=unconfigured`，尚无真实地区资源、人工值班人、通知发送器或演练结果。因此技术链路可验收，但安全上线 Gate 不通过。

## 行动建议与事件闭环合同

### Action

字段固定为 `type`、`module`、`page`、`params`、`reason`、`expires_at`。一轮最多一个 action；module 只允许 M1、M2、M3、M5。服务端白名单版本为 `prototype-v1-pending-java-confirmation`，当前页面路径仍需 Java/前端冻结，不能当作最终生产路径。

非法 module、M9、未知页面、任意 URL、额外字段、危险 params、缺少 reason 或非法 expires_at 会清空 action 并记录 `action_policy_violation`，不影响安全回复。

### Recommendation

`ai_action_recommendations` 保存 recommendation_id、user_id、session_id、module、action_json、reason、status、source_message_id、expires_at、created_at。状态固定为 recommended、accepted、completed、declined、expired。

允许转换：

- recommended -> accepted / declined / expired
- accepted -> completed / declined
- completed、declined、expired 为终态

过期行动不得接受或完成，非法转换拒绝。

### Java Action Event

事件字段固定为 event_id、recommendation_id、user_id、module、event_type、occurred_at、metadata。event_id 是唯一键；重复事件返回已有状态，不重复写入、反馈或记忆。

Java/小程序拥有真实点击和完成事实。Python 只验证合同、消费事件和形成受限反馈。

### 完成与拒绝

完成反馈引用 metadata 中实际 activity 和 effort，不使用空泛“你很棒”，不承诺疗效。授权 personalization 后，将完成摘要写入 action_summary 记忆；未授权时不写长期记忆。

拒绝事件最多记录 200 字符的受限原因。相同用户和 module 在 `ACTION_DECLINE_COOLDOWN_HOURS` 内不得重复推荐，下一轮应先理解阻碍。

## 长期记忆合同

### 记忆类型

| memory_type | 中文含义 |
|---|---|
| `profile` | 用户明确提供的基本资料 |
| `family_relationship` | 用户明确描述的家庭关系 |
| `interest_preference` | 兴趣与偏好 |
| `key_event` | 用户明确确认的重要事件 |
| `emotion_trend` | 有来源和时效的情绪趋势摘要 |
| `action_summary` | 已完成或拒绝的行动摘要 |
| `conceptualization_clue` | CBT 概念化线索，不是诊断事实 |

### 授权

默认 `personalization=false`，不写入也不检索。撤回后下一次请求立即停止检索并清除本地上下文缓存。高敏感候选还需要 `sensitive=true`；Java 后端仍拥有业务真值，Python 记忆不得反向覆盖 Java 摘要。

### 候选与写入

模型或规则只能产生 `MemoryCandidate`，不能直接写库。服务端校验类型、正文长度、confidence、source_message_id、consent_scope、明确表达标记和敏感授权。推测候选必须达到 `MEMORY_CONFIDENCE_THRESHOLD`，不得把“可能喜欢”写成确定事实。

同一用户、类型、来源和授权范围执行受控更新；同一用户、类型和正文哈希去重。不同 user_id 永远分开查询。会话摘要与长期记忆分开，不保存无限聊天历史。

### 检索

检索强制使用 user_id 和 personalization 授权，默认最多 6 条、2000 字符，并过滤未生效、过期和已删除记录。当前用户消息在 Prompt 中位于记忆之前；记忆放入 `untrusted_memory`，不能提升为 system 指令。

### 查看、纠正与删除

用户可查看、纠正、软删除或彻底删除自己的记忆。纠正和删除会清理缓存并删除/置空向量。软删除立即清空正文和哈希，只保留 tombstone；彻底删除移除关系记录。`ai_memory_audit` 仅保留 memory_id、user_id、动作和时间，不保存被删除正文。删除操作幂等。

### 数据库

Docker PostgreSQL 使用 `migrations/003_ai_memories.sql`，包含 `ai_memory_consents`、`ai_memories` 和 `ai_memory_audit`。访问权限撤销 PUBLIC 后授予项目角色。embedding 维度沿用 1536；真实 embedding 回填仍属于未完成外部链路。

## 检验日志与优化合同

### Inspection Log

每个 response 只写一条 `ai_inspection_logs`，request_id 唯一。记录 message_id、加盐 user_hash、最终回复 SHA-256 引用、Inspector 字段、issues、延迟、主/副模型、Prompt 版本、供应商 usage、error_pattern、lesson_ref 和时间。

不记录 API Key、系统 Prompt、真实 user_id、完整危机文本或完整候选回复。供应商不返回 token/费用时保存 null，不做虚假估算。

日志写入失败不改变用户安全回复，只追加 `inspection_log_write_failed` 并进入 request_id 幂等重试队列。

### Lessons

`ai_lessons` 使用内容 SHA-256 去重，状态为 pending、approved、rejected。只有 approved 可被检索；审批必须包含 reviewer 和 reason，未审批内容不改变线上 Prompt。

### Prompt Patches

`ai_prompt_patches` 按 error_pattern 创建候选，默认 pending。审批必须记录 reviewer、reason、test_report 和 target_version。批准不等于自动发布；发布必须新增不可变 Prompt 版本文件，并用 `scripts/运维工具.py sync-prompts` 校验/同步哈希。

### 审核工具

`scripts/运维工具.py quality-review` 默认只显示 error_pattern 聚合、待审数量和危机事件索引，不显示高敏感原文。运营流程文档规定抽样、审批、回归、发布和回滚证据。
