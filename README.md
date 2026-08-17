# 小辽智能体

这是一个可以独立在线对话的 Python Agent 原型，暂不接微信、Java 后端或前端。运行时必须同时配置 DeepSeek 和 Qwen 的真实 API Key。

当前链路：

```text
用户消息
  -> 同一份 RAG 检索（CBT 知识库、教训、提醒、实时事实）
  -> DeepSeek V4 Flash 主 Agent 生成候选回复
  -> Qwen 3.7 Flash 副 Agent 同读这份 RAG 后质检主 Agent 回复（关闭思考模式）
  -> 危机/安全硬拦截，或软性问题重写一次
  -> 返回回复、行动建议和检验信息
```

## 目录

```text
xiaoliao_agent/
├── knowledge/CBT知识库_Agent版.md  # 已纳入的 CBT 知识库
├── knowledge/lessons.md           # 初始教训库，可持续追加
├── xiaoliao_agent/                # Agent 核心代码
├── tests/                         # 本地测试
├── .env.example                   # 模型配置模板
├── requirements.txt
└── 运行/run_agent.py              # 交互式命令行入口
```

## 1. 安装

建议使用 Python 3.10+：

```bash
python -m venv .venv
# Windows PowerShell
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## 2. 配置真实模型

推荐直接双击 `配置API.bat`。脚本会在本机隐藏读取两个 API Key，并自动生成 `.env`。密钥不会显示在屏幕上，也不会写入示例配置。

配置完成后双击 `检查API连接.bat`，它会分别向两个模型发送最小测试消息。连接测试会产生少量 API 调用费用。

也可以手动复制 `.env.example` 为 `.env`，填入两个模型的 API Key。默认模型名按当前项目约定写为：

```env
DEEPSEEK_MODEL=deepseek-v4-flash
QWEN_MODEL=qwen3.7-flash-2026-07-15
INSPECTOR_ENABLE_THINKING=false
```

这是已经在线验证通过的 API 模型 ID。产品名称仍是 DeepSeek V4 Flash / Qwen 3.7 Flash；API ID 中包含连字符。两个接口使用 OpenAI 兼容的 `/chat/completions` 格式。

### 模型组合建议

主模型固定为 `deepseek-v4-flash`（用户确认不更换）。副模型继续使用
`qwen3.7-flash-2026-07-15`，但必须显式设置 `INSPECTOR_ENABLE_THINKING=false`：

- Qwen3.7 系列默认开启思考模式，Inspector 延迟实测约 5-16 秒，是全链路主要瓶颈。
- Inspector 只做结构化安全质检（危机、医疗边界、意图、适老），不需要长思考链，
  关闭思考模式可显著降低延迟，同时保留质检能力。
- 主、副模型共用同一份 RAG 上下文：副 Agent 依据知识库判断候选回复是否遵循
  CBT 指引、是否遗漏关键原则；RAG 为空时仍按通用安全陪伴原则检查。
- 快检不再单独做最终裁决：只要快检出现 `issues`、软性不合格、非 `none` 的
  `error_pattern`，或主模型返回非 `none` 的 `risk_hint`，会自动升级为开思考的
  强检复核（`INSPECTOR_ESCALATE_ON_ISSUES=true`）。普通对话保持快检，存疑内容
  才付出额外延迟。
- 若后续仍需要更极致的 Inspector 延迟，可申请智谱 BigModel Key 后切换
  `QWEN_BASE_URL=https://open.bigmodel.cn/api/paas/v4`、
  `QWEN_MODEL=glm-4.7-flashx`（GLM-4.7 系列最低延迟档，付费高速档）。
  切换后必须重跑安全与 55 条真实评估，不能只测延迟。

## 3. 开始在线对话

```bash
python 运行/run_agent.py
```

Windows 也可以直接双击：

- `开始真实模型对话.bat`（需要先配置 `.env`）
- `配置API.bat`（在本机安全输入两个 Key）
- `检查API连接.bat`（真实调用两个模型验证连接）

单句测试：

```bash
python 运行/run_agent.py --message "我最近什么都不想做"
python 运行/run_agent.py --stream --message "我最近什么都不想做"
```

`--stream` 使用安全流式输出：主模型和 Inspector 全部通过后，回复按 4 字符片段逐段
显示，未过审内容不会提前出现。交互模式同样支持 `--stream`。

调试检索来源和质检结果：

```bash
python 运行/run_agent.py --debug
```

## 4. 接下来如何接 Java

生产合同为 `POST /v1/chat`。端口唯一配置来源是 `.env` 的 `API_PORT`，默认 `8081`；Swagger 在 `http://127.0.0.1:<API_PORT>/docs`。

调用前必须设置 `API_TOKEN`，请求头使用 `Authorization: Bearer <API_TOKEN>`。调试 Token 使用可选的 `API_DEBUG_TOKEN`，只有它才能请求 `debug=true` 的调试字段。

`POST /v1/chat` 请求严格字段：

```json
{
  "user_id": "user_001",
  "session_id": "session_001",
  "message": "我最近什么都不想做",
  "context": {
    "consent": {"personalization": true},
    "user_summary": "用户喜欢戏曲，最近完成过 2 次脑力游戏"
  },
  "debug": false
}
```

`context` 只接受 `consent` 和 `user_summary`，不接受 `system_prompt` 或任意扩展指令。响应固定包含 `session_id`、`reply`、`intent`、`action`、`blocked`、`crisis_detected`、`safety_violation`、`rewritten`；`debug=true` 且调用方有调试权限时额外返回 `debug.inspection` 和 `debug.sources`。

外部 `intent` 只允许 `chat`、`checkin`、`game`、`exercise`、`assessment`、`community` 六值；内部 `emotion_support` 等意图由 Python 映射，Java 不感知。

旧 `POST /chat` 已标记 deprecated，仅作为有期限的兼容入口。移除条件：Java 和企微全部迁移到 `/v1/chat` 并完成一个发布周期验证。

安全流式输出：`POST /v1/chat/stream` 使用 SSE，在完整安全链路（主模型 + Inspector）通过后才流式返回最终回复，不会提前把未过审 token 发给用户。

完整合同见 [openapi.json](openapi.json)，错误码见 [docs/API错误码表.md](docs/API错误码表.md)，curl 与 Java 示例见 [docs/API联调示例.md](docs/API联调示例.md)。

### 用户授权、摘要与删除

`POST /v1/users/consent` 设置个性化授权；`GET /v1/me/summary` 返回当前授权、个人记忆摘要和最近对话事件；`POST /v1/privacy/delete-request` 发起删除，会清理用户记忆、授权和对话事件。`/v1/chat`、`/v1/chat/stream` 与兼容入口 `/chat` 每轮都会写入 `conversation_events`，消息正文只保存 `sha256` 引用，不保存原文。

## 5. RAG 与延迟

知识库按 Markdown 二级到四级标题分节，正文超过 1800 字符时按 1800 字符切分并保留 180 字符重叠；chunk 使用稳定内容哈希去重。当前知识库只保留
`knowledge/CBT知识库_Agent版.md` 与 `knowledge/lessons.md` 两个来源。

RAG 评估用例见项目根目录 `rag_cases.json`，运行 `python run_rag_eval.py --offline`
验证；指标是 expected_terms 命中率，不是人工语义准确率。

延迟配置：

```env
AGENT_MAX_TOKENS=400
INSPECTOR_MAX_TOKENS=256
INSPECTOR_ENABLE_THINKING=false
INSPECTOR_ESCALATE_ON_ISSUES=true
AGENT_TIMEOUT_SECONDS=60
RAG_PARALLEL_ENABLED=true
```

真实分阶段实测显示主要耗时在 Qwen Inspector 的思考模式，关闭后详细数据见 [docs/延迟实测记录.md](docs/延迟实测记录.md)。

## 5.1 实时事实能力（联网检索 / 时间）

大模型本身不包含实时数据，因此 Agent 增加联网检索层，只对明确的事实问题触发：

- 当前时间：直接读取本机北京时间，不联网
- 天气、附近地点、农历/节气/节假日、新闻简讯、药品通用科普：构造检索词后走
  `WEB_SEARCH_PROVIDER`。默认 `dashscope`，复用现有 `QWEN_API_KEY`，通过百炼
  OpenAI 兼容 Chat Completions 的 `enable_search` 开启内置联网搜索
  （模型 `WEB_SEARCH_DASHSCOPE_MODEL`，默认 `qwen3.7-flash-2026-07-15`）；
  也可换成 `serper` / `brave` / `tavily`。默认城市 `LIVE_DEFAULT_CITY`（默认沈阳）
- 查询结果作为“不可信参考资料”注入主模型，最后仍经过安全质检
- `WEB_SEARCH_API_KEY` 未配置时，联网类问题自动降级为“看不到实时信息”，不报错；
  查询失败或超时同样自动降级，不拖慢普通对话
- 联网检索结果按“提供商+检索词”缓存 30 分钟，重复提问立即返回，不再重复付费和等待

配置见 `.env.example` 的 `LIVE_*` / `WEB_SEARCH_*` 项。真实用户位置由 Java/企微侧后续提供，
当前用配置城市代替，不代表 GPS 定位。

## 5.2 便民与安全工具

- 提醒设置：识别“每天上午8点提醒我吃药”等话术，本地记录提醒并注入确认上下文；
  重复请求幂等；只做本地记录，不修改系统、不推送，真实推送待 Java/企微。
- 药品科普/健康常识：只回答通用公开常识，不给剂量、不开药、不诊断；个人用药决策
  问题统一引导咨询医生/药师，由确定性规则处理。
- 诈骗识别：转账/汇款、银行卡/验证码、公检法、中奖/刷单等风险话术由确定性规则
  拦截并给出防骗提示。

知识库导入与向量回填仍使用：
`python import_knowledge.py`、`python backfill_embeddings.py`，默认只处理
CBT 知识库与 lessons.md。

## 5.3 企微员工群每日签到提醒

小辽可以通过企业微信“群机器人 Webhook”给内部员工群每天定时发送签到提醒。
员工群不经过微信客服通道，不受“48 小时回复窗口、最多 5 条”的限制，可以
每天固定时间推送。功能默认关闭，配置项见 `.env.example` 的
`WECOM_CHECKIN_REMINDER_ENABLED` 等变量，详细策略见
[docs/签到提醒策略.md](docs/签到提醒策略.md)。提醒按 PRD 中 M1 情绪签到
定位设计：温和、不催促，作为养成每日记录习惯的固定线索。配置
`WECOM_CHECKIN_GROUP_USER_SCHEDULE_PATH` 后可按每个用户自己的习惯时间
分别提醒，到点通过企业微信应用消息发送私信，不在群里 @ 用户；需要
`WECOM_CORP_ID`、`WECOM_AGENT_ID`、`WECOM_AGENT_SECRET`。未配置用户级
时间表时回退到群统一时间。

开启后 API 进程会每 60 秒检查一次北京时间，到点发送一次；同一日期同一
时间点幂等，发送失败不记录成功并在补发窗口内重试。手动验证：

```bash
python 运行/run_checkin_reminder.py --once
```

## 6. 重要边界

- 不做心理/身体疾病诊断，不给药物或治疗方案。
- 检测到自伤、自杀、极度绝望等信号时，走固定危机话术，不使用普通生成回复。
- 正式运行不提供 Mock/离线模式；缺少任意 API Key 时直接拒绝启动。
- 知识库原文来自项目资料，正式上线前需要完成版权、隐私、热线和人工转介审核。
## 生产容器

生产 Compose 包含 `postgres`、`redis`、一次性 `migrate`、多 worker
`agent-api` 和唯一 `checkin-worker`。Python API 不发布宿主机端口，由受信 Java
网关通过内部网络访问。

```bash
docker compose config -q
docker compose build
docker compose up -d agent-api checkin-worker
docker compose exec agent-api python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8081/health/ready').read())"
```

生产前从 `.env.example` 建立部署环境配置。`API_TOKEN`、网关 HMAC、隐私 HMAC、
质量盐和数据库密码必须各不相同；不要提交任何 `.env` 文件。HMAC 轮换时提升
`PRIVACY_HMAC_KEY_VERSION`，并把仍在读取期内的旧密钥放入
`PRIVACY_HMAC_PREVIOUS_KEYS`。

运行时指标位于需 service token 的 `/metrics`；日志为 JSON，禁止记录请求正文、
用户 ID、记忆内容或 token。

### 发布检查

```bash
python scripts/verify_release.py --check
python scripts/verify_release.py --gate
```

`--gate` 只验证可自动化项目。真实模型预算、7 天灰度、企微闭环、危机演练和
Java 合同仍须负责人提供外部证据。

### 加密备份与恢复演练

备份凭据只从 `KNOWLEDGE_DATABASE_URL` 读取；目标目录必须是专用目录，最终制品
使用 age 加密并原子落盘。

```bash
python scripts/backup_database.py --destination /secure/backups --age-recipient age1...
AGE_IDENTITY_FILE=/secure/keys/backup.agekey \
python scripts/restore_database.py \
  --backup /secure/backups/xiaoliao-20260817T000000Z.dump.age \
  --target-database xiaoliao_restore_drill \
  --confirm-target xiaoliao_restore_drill
```

恢复脚本拒绝覆盖源数据库；演练只能指向一次性隔离数据库。
