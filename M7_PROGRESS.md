# M7 项目执行状态

> 本文件由每轮编码 AI 更新。只记录已经由代码、测试或外部联调证据证明的事实。

## 当前阶段

- 最后完成步骤：RAG 补全（记忆向量回填 + 知识库扩展 + 教训回灌 + RAG 专项评估）
- 当前状态：RAG 专项实现完成并通过监督验收；真实运营数据仍待外部确认
- 下一步：受限试点与外部 Gate 确认

## 固定事实

- 项目根目录：C:\Users\Administrator\Desktop\小辽智能体_Python_Agent
- 需求文档：C:\Users\Administrator\Desktop\小辽M7智能体完整需求文档_token-min.txt
- 主模型：deepseek-v4-flash
- 副模型：qwen3.7-flash-2026-07-15
- 当前接口：POST /v1/chat，端口唯一来源 `API_PORT`（默认 8081）
- 目标接口：POST /v1/chat，正式冻结待用户与 Java 负责人确认

## 步骤状态

| 步骤 | 状态 | 测试或证据 | 阻塞 |
|---|---|---|---|
| 00A 基线与安全热修 | 完成（原型级） | 基线时 `python -m pytest -q`：21 passed，2 warnings | - |
| 00B 真实模型基线 | 完成（10/10 基线通过） | `eval_v1.json`：10 条全部完成，无失败；根因是 Docker 未启动导致 psycopg 挂死，已修复（connect_timeout + 优雅降级） | - |
| 01 CBT 知识库 | 完成 | `python -m pytest -q`：100 passed；Docker pgvector 0.8.6；78 chunks 与 78 条 1536 维 embedding 已回填；运行时混合检索验证通过 | - |
| 02 Prompt 规范 | 完成 | `python -m pytest -q`：36 passed；Prompt 1.0.0，回滚点 0.9.0；9 条高风险回归 | 人工标签仍为 needs_review |
| 03 主 Agent | 完成 | `python -m pytest -q`：55 passed；严格 JSON、action 白名单、错误分类、输入边界和版本化降级均有回归 | - |
| 04 副 Agent | 完成 | `python -m pytest -q`：68 passed；Prompt 1.1.0；严格九字段、错误枚举、一次重写、二次软失败和 lesson 待审均有回归 | lesson 仅内存队列，持久化待第 8 步 |
| 05 安全与危机 | 实现完成，安全上线阻塞 | `python -m pytest -q`：87 passed；18 条红队；危机事件数据库集成通过 | route、地区资源、人工值班和真实演练未配置 |
| 06 长期记忆 | 完成 | `python -m pytest -q`：98 passed；授权、去重、同源更新、隔离、撤回、纠正、软/硬删除和 PostgreSQL 往返均有测试；006 向量迁移已应用 | 真实运营记忆数据待后续回填验证 |
| 07 行动闭环 | 实现完成，外部合同待冻结 | `python -m pytest -q`：112 passed；四模块、状态机、事件幂等、正反馈、拒绝冷却和数据库往返均有测试 | Java/前端页面与 params 未冻结 |
| 08 日志与优化 | 完成 | `python -m pytest -q`：125 passed；全终态日志、失败重试、lesson/patch 审批、Prompt 哈希和 PostgreSQL 往返均有测试 | 跨周期趋势需后续真实数据 |
| 09 API 与 Java | 实现完成，生产 Gate 阻塞 | `python -m pytest -q -k "not db"`：153 passed，5 deselected；Java 示例本地假 Agent 服务调用 HTTP 200 | 鉴权/端口/意图正式冻结待用户与 Java 负责人确认；真实 Java 后端联调待外部资源 |
| 10 评估监控灰度 | 方案完成，真实灰度待执行 | 55 条固定评估集；离线 55/55；真实主链路评估 53/55（96.36%）；RAG 真实向量 22/22 | 真实 7 天灰度待执行；监控阈值待确认 |
| 11 企微联调发布 | 适配器与方案完成，真实联调待执行 | 18 条企微接入契约测试；非 DB 全量 185 passed | 企微凭据、发送权限、Java 地址、真实值班/热线和获批测试账号待确认 |
| 99 项目终验 | 完成（可进入受限试点） | `python -m pytest -q`：209 passed，2 warnings；完整终验报告已生成 | 真实灰度、企微闭环、危机转介和监控阈值 BLOCKED |
| RAG 补全 | 实现完成，监督验收通过 | `python -m pytest -q`：209 passed；006 迁移已应用；知识库 DB 110/110 已 embedding；`rag_runs/run-completed` 22/22；记忆向量 DB 往返与缓存测试通过 | 真实 approved lesson、地区资源热线/值班和真实 embedding 评估待外部确认 |

## 最新测试

- 命令：`python -m pytest -q --tb=short`
- 结果：243 passed，2 warnings
- 日期：2026-08-07
- 注：本次全量 243 passed；新增联网搜索提供商/缓存/路由测试、提醒、防诈骗等工具测试；
  Prompt 升级到 1.3.0。5 个 DB 集成测试全部通过；离线评估 55/55；企微接入契约
  测试 18/18；RAG 离线 22/22、真实向量 22/22。2026-08-07 已执行
  `import_knowledge.py`（inserted=11）与 `backfill_embeddings.py`（11/11），
  知识库 DB 110/110 有 1536 维向量。

## eval_v1 基线结果 (2026-08-07)

- 10/10 通过，无失败，无 Mock
- 安全拦截 3 条（医疗边界、危机、Prompt注入），确定性预检 10-19ms
- 7 条正常对话通过完整双 Agent 管道，延迟 9-20s（DeepSeek V4 Flash 响应偏慢）
- case_01 出现 `AGENT_INVALID_JSON` 但降级兜底正常触发，未暴露异常给用户

## 延迟优化（2026-08-07）

- 根因定位：Qwen3.7 系列默认开启思考模式，Inspector 非流式质检被 `enable_thinking=true`
  拖慢到 5-16 秒，是全链路主要瓶颈。
- 组合：主模型保持 `deepseek-v4-flash`；Inspector 保持 `qwen3.7-flash-2026-07-15`，
  新增 `INSPECTOR_ENABLE_THINKING=false`（默认 false）。
- 实测：Inspector 降至 0.7-0.9 秒，全链路降至约 2.5-3.2 秒；三次真实调用 error=none。
- 备选：智谱 `glm-4.7-flashx` 为 GLM-4.7 系列最低延迟档，但需要新 Key，且切换后必须
  重跑安全与 55 条真实评估，暂未采用。
- 流式输出：`运行/run_agent.py` 新增 `--stream`，走 `agent.chat_stream` 安全缓冲流，主模型
  与 Inspector 全部通过后才按 4 字符片段输出，未过审内容不会提前出现；真实 `--stream`
  调用 1 条验证通过。API 侧 `/v1/chat/stream` SSE 保持原有安全缓冲设计。
- 质检质量核对（2026-08-07）：关闭 Inspector 思考模式后，确定性预检/后检离线评估
  `eval_runs/run-offline-postthinkingoff` 为 55/55，硬性安全漏放 0；安全/Inspector/
  Prompt/质量定向测试 31 passed。同一模型（qwen3.7-flash）开/关思考的真实 A/B 尚待
  用户批准费用后执行，此前 55 条真实运行（qwen3.7-max）不能作为同模型基线。
- 质检增强（2026-08-07）：新增 `INSPECTOR_ESCALATE_ON_ISSUES=true` 两层质检。快检
  （关思考）只作为第一层；出现 issues、软性不合格、非 none 的 error_pattern 或主
  模型 risk_hint 时，自动升级为开思考强检复核。普通对话保持快检，存疑用例才付额外
  延迟；该行为有 5 条定向测试锁定，全量测试 211 passed。
- 实时事实能力（2026-08-07）：按用户要求改为“联网搜索为主”。当前时间用本机时钟；
  天气/附近/农历/节假日/新闻/药品科普统一走 `WEB_SEARCH_PROVIDER`
  （默认 `dashscope`，复用 QWEN_API_KEY，百炼 OpenAI 兼容 Chat Completions
  `enable_search` + `search_options`，模型 `qwen3.7-flash-2026-07-15`），结果作为
  不可信资料注入并仍过安全质检；已删除被替代的 Open-Meteo、Overpass、zhdate、
  节假日接口和 RSS 实现。真实联网天气已端到端验证（沈阳 8 月 7 日多云转晴
  16-28 度）。单次联网检索实测：农历 11 秒、天气 12 秒、附近 16 秒、药品 22 秒、
  新闻 23 秒、防诈骗案例 39 秒；结果缓存 30 分钟，重复提问约 0 秒。普通对话不触发
  联网。
- 便民与安全工具（2026-08-07）：提醒设置（本地 ReminderService，幂等，Prompt 1.3.0
  明确提醒确认不属于医疗建议）；药品科普边界（通用公开常识可答、剂量/开药/诊断
  一律拦截，真实验证“阿司匹林是做什么的”可正常回答）；诈骗识别（转账/银行卡/
  验证码/公检法/中奖/刷单确定性拦截 + 防骗话术 + 案例知识库）；老歌戏曲/故事谜语
  （知识库）。Prompt 已发布 `1.3.0`（回滚点 `1.2.0`）。

## RAG 评估结果 (2026-08-07)

- 22/22 通过（离线词法检索）
- 整体命中率 0.742
- 知识库：99 chunks / 4 源（CBT 73 + 适老 14 + 资源 7 + 教训 5）
- 分源多样化检索已启用（diverse=True），每源保证至少 1 个代表

## 已知问题

- 当前目录不是 Git 仓库，未初始化 Git。
- pytest 警告来自 Starlette/httpx 兼容性、pytest-asyncio 弃用提示，以及 Windows pytest 缓存目录权限问题。
- DeepSeek V4 Flash 首轮响应偶有 JSON 格式不合法（case_01），降级兜底正常触发但回复质量有损。建议排查 Prompt 中的 JSON 约束是否足够严格。
- case_09（"我家人总是不听我说话"）耗时 19.9s，接近超时边缘，需持续关注 DeepSeek 延迟波动。
- `POST /v1/chat` 已实现并有合同测试；旧 `POST /chat` 标记 deprecated。真实 Java 后端联调和鉴权/端口/意图的正式冻结仍待外部确认。
- 当前 PostgreSQL/Docker 可达，5 个 DB 集成测试已通过；若数据库再次不可达，Agent 会自动降级到内存模式并输出 warning。
- 真实模型 55 条主链路评估已执行（`eval_runs/run-real-main-v2`）：53/55，硬性安全漏放 0；2 例失败为模型输出 invalid JSON / invalid action。
- 监控告警阈值全部标为“待确认”，未接入真实平台前不宣称生产监控已上线。
- 7 天灰度方案已完成，真实试点用户、每日检查和 7 天复盘尚未执行。
- 企微回调验签、解密和消息闭环由外部团队负责，Python 侧只提供无发送职责的适配器；真实企微凭据、Java 地址和发送权限均未配置。
- 危机演练记录保持“未执行”，未填写编造的热线、值班人或通过结论。
- 终验结论为“可进入受限试点”；真实 55 条模型评估、真实灰度、企微闭环、危机转介和监控阈值均为 BLOCKED，详细判定见 `M7_项目终验报告.md`。
- `.env` 含非空真实密钥，且当前目录不是 Git 仓库；发布打包前必须确认 `.env` 不进入任何制品。
- 联网检索延迟：百炼联网搜索单次约 11-39 秒（看查询类型），加上主模型与质检，
  联网类问题端到端约 15-42 秒；同一检索词缓存 30 分钟，重复提问约 0 秒。普通对话
  不触发联网仍约 2.5-3.2 秒。已关闭 `forced_search` 并放宽超时到 45 秒以降低失败，
  如需进一步提速需权衡时效性（缩短 freshness）或换更快的搜索提供商。
- 知识库数据库已更新：新增 健康常识与药品边界 / 诈骗案例库 / 老歌戏曲与休闲 三个
  来源共 11 块，`ai_knowledge_chunks` 当前 110 行、110/110 有向量；来源 7 类。
- 延迟优化前记录：`eval_v1` 基线 7 条正常对话为 9-20s，`case_09` 曾耗时 19.9s；
  关闭 Inspector 思考模式后全链路实测约 2.5-3.2s，历史记录保留供对比，不代表当前配置。

## 决策记录

- 00A 保持最小范围：只补齐明确诊断请求的医疗边界规则和回归证据，不提前改造 API、数据库或 Agent 架构。
- README 已核对并明确当前原型为 8081 + `POST /chat`，目标为 `POST /v1/chat`，未声称目标合同已实现。
- 00B 离线检查：`.env` 中 `DEEPSEEK_API_KEY`、`QWEN_API_KEY` 及对应模型 ID 均为存在；未运行 `检查API连接.bat`，未产生真实调用费用。
- 00B 真实连接检查：2026-08-06 DeepSeek/Qwen 均返回 `OK`；首条 `POST /chat` 基线请求失败，耗时约 12125 ms，已保存脱敏证据并停止后续 9 条。
- 00B 产物：[eval_v1.json](eval_v1.json)、[基线检查报告.md](基线检查报告.md)；未使用 Mock，未宣称 10 条基线完成。
- 01 离线实现：稳定内容哈希 chunk_id、标题路径/来源/版本元数据、词法与可选向量融合、向量故障回退、内存导入仓库、PostgreSQL 迁移和 6 类 RAG 回归已加入。
- 01 Docker/embedding 验证：`pgvector/pgvector:pg18` 绑定 `127.0.0.1:55432`，扩展版本 `0.8.6`；`ai_knowledge_chunks` 为 78 行且 78 行均为 1536 维 embedding；Qwen `text-embedding-v4` 回填和运行时向量/词法融合检索已验证。
- 02 Prompt 规范：主 Agent、Inspector、重写 Prompt 使用不可变文件 registry；默认版本 `1.0.0`，回滚点 `0.9.0`，未知版本启动即拒绝；已加入做/不做清单、适老词表、注入防护、9 条高风险回归和版本差异工具。
- 03 主 Agent：主输出使用严格 Pydantic 合同；非法 action 清空并记录；历史、RAG 和记忆钩子有长度与角色限制；模型错误按超时、网络、HTTP、限流和非法响应分类；内部结果记录 request_id、主模型和 Prompt 版本。
- 04 副 Agent：Inspector 使用严格九字段 Pydantic 合同和固定 error_pattern 维度；解析/超时/合同失败按不安全处理；软失败只重写一次，二次软失败返回保守降级；lesson 只进入 pending 内存审核队列，审批需 reviewer 和 reason；协同 Prompt 发布为 `1.1.0`，回滚点 `1.0.0`。
- 05 安全链路：风险类别固定为 crisis、medical_boundary、unsafe_content、normal；规则保留稳定 ID 和最小证据码；固定话术版本化；Docker 数据库已执行 ai_crisis_events 迁移；通知失败重试且按 event_id 幂等。`CRISIS_ROUTE=unconfigured`，未编造热线或演练结果。
- 06 长期记忆：默认未授权不读不写；候选与保存分离；7 类记忆固定；PostgreSQL 已执行 ai_memory_consents、ai_memories、ai_memory_audit 迁移；支持查看、纠正、撤回、软删除、彻底删除、缓存和向量清理，跨用户及并发隔离有回归。测试合成数据已清理。
- 2026-08-07 模型组合：主模型不更换 `deepseek-v4-flash`；Inspector 使用
  `qwen3.7-flash-2026-07-15` 并显式关闭思考模式。原因：Qwen3.7 默认思考模式导致
  Inspector 5-16s；结构化安全质检不需要长思考链。备选 `glm-4.7-flashx` 需外部 Key
  且要重跑评估，未静默切换。
- 2026-08-07 文件整理：删除 Python/pytest 缓存目录和空目录 `calendar_data`；根目录
  9 份合同/流程/模板工作文档移入 `docs/`。未删除代码、测试、数据、配置或评估证据，
  整理后全量测试仍为 243 passed。
- 07 行动闭环：action 使用六字段严格合同和原型白名单；PostgreSQL 已执行 ai_action_recommendations、ai_action_events 迁移；event_id 幂等、状态转换、过期拒绝、拒绝冷却、具体正反馈和授权 action_summary 记忆均有测试。页面与 params 仍待 Java/前端冻结。
- 08 质量数据：PostgreSQL 已执行 ai_inspection_logs、ai_lessons、ai_prompt_patches、ai_prompt_versions 迁移；成功、模型/Inspector 失败、危机和重写终态均只写一条隐私安全日志；写入失败进入幂等重试；lesson 哈希去重，patch 审批需 reviewer、理由、测试报告和目标版本；12 条 Prompt 版本哈希已同步。
- 00B 修复：2026-08-07 定位 eval_v1 首条失败根因为 Docker 未启动 → PostgreSQL 55432 不通 → psycopg.connect() 无超时挂死。修复：所有 Postgres 仓库 connect 加 connect_timeout=5；XiaoliaoAgent.__init__ 新增 _db_reachable() 预检，数据库不可达时自动回退内存模式并输出 warning。新增 run_eval_baseline.py，10 条基线全部通过。
- 09 API 合同：`/v1/chat` 严格 Schema、外部六值 intent、Bearer 鉴权、`API_DEBUG_TOKEN` 调试权限、主体+用户双维度限流、可信/生成 request_id、单进程幂等协调和 401/403/409/422/429/502/504 稳定错误码已实现并测试锁定；`/v1/chat` 不使用进程内 ConversationStore，Agent 只接收 Java 授权摘要。
- 09 OpenAPI/示例：`openapi.json` 含 bearerAuth、V1ChatRequest/V1ChatResponse/V1DebugInfo/ApiError 和弃用 `/chat`；`docs/API错误码表.md` 与 `docs/API联调示例.md` 已生成；Java 11+ 示例已 `javac` 编译，并针对本地假 Agent 测试服务完成 HTTP 200 调用。
- 09 兼容与移除条件：旧 `/chat` 只接受 user/assistant 历史，标记 deprecated；移除条件为 Java 与企微全部迁移到 `/v1/chat` 并完成一个发布周期验证。
- 10 评估集：`eval_suite_v1.json` 共 55 条，覆盖普通陪伴、情绪困扰、自动思维、行为退缩、关系破裂、医疗边界、危机、行动引导、适老表达、越权指令和 Prompt注入 11 类；每条含 case_id、category、input、context、expected_constraints、expected_intent、allowed_actions、forbidden_phrases、expected_risk、severity 和 needs_review 人工标签，无真实用户隐私。
- 10 评估脚本：`run_eval_suite.py` 支持 `--offline` 确定性预检、`--direct/--http` 真实模型和 `--resume` 断点续跑；运行目录不可变，final 结果不可覆盖；真实模型模式必须显式提供 `--real-approved-by`。离线运行 `eval_runs/run-offline-v1` 为 55/55，硬性安全漏放 0。
- 10 指标与门：一次通过率、重写率、拦截率、硬性安全漏放、误拦截候选、RAG 命中率、p50/p95、模型成功率、token 和费用均已计算；发布目标为一次通过率 >85%、重写率 <15%、硬性安全违规 0。
- 10 监控与灰度：`monitoring/alert_rules.json` 覆盖模型连续失败、p95、费用、危机拦截失败、转介失败、队列积压、5xx 和知识库降级，阈值均标“待确认”；`docs/7天灰度方案.md` 给出 10-20 位授权试点、每日检查、暂停/退出条件、责任角色、数据删除和复盘表，状态保持“真实灰度待执行”。
- 11 企微接入：`xiaoliao_agent/wecom.py` 只接收验签/解密后的事件并返回回复载荷，不负责发送；回调验签、时间窗、解密失败、事件去重、乱序、超时和 `Idempotency-Key=事件ID` 均有契约测试。
- 11 主动问候：`GreetingPolicy` 默认关闭，支持全局开关、单用户关闭、免打扰、最近活跃、情绪趋势、每日频率和授权要求；撤回授权后主动问候被阻止且不再读取个性化记忆。
- 11 行动与危机：M1/M2/M3/M5 完成事件映射到既有行动合同，未知模块拒绝，重复事件幂等且不新增记忆；危机转介按地区/授权/值班配置读取，缺少热线或值班人时记录 `crisis_contact_missing` 告警，不编造联系方式。
- 11 发布文档：`docs/企微联调检查单.md`、`docs/危机演练记录.md`（未执行）、`docs/上线回滚方案.md` 和 `docs/主动问候策略.md` 已生成；真实企微闭环、危机转介和 7 天灰度均未宣称完成。
- 99 终验：全量 `python -m pytest -q` 209 passed，2 warnings；DB 集成 5/5，离线评估 55/55，红队 21/21，合同/评估/企微定向 64/64。结论为“可进入受限试点”，真实灰度、企微闭环、危机转介和监控阈值 BLOCKED；终验报告已写入 `M7_项目终验报告.md`，每个未通过项已指向对应步骤。
- RAG 记忆向量：PostgresMemoryRepository 新增 `vector_search()`、`update_embedding()`、`find_embeddings_missing()`、`count_embeddings()` 方法；MemoryService 新增 `embed_client` 和语义搜索 `get_context(query=...)`；迁移 `006_ai_memories_vector.sql` 创建 IVFFlat 向量索引；新增 `backfill_memory_embeddings.py` 回填脚本。
- RAG 知识库扩展：新增 `knowledge/适老生活场景.md`（14 chunks，覆盖退休适应、代际关系、社交孤独、身体变化、丧偶等）和 `knowledge/地区资源参考.md`（7 chunks，覆盖热线、专业帮助、用药安全、社区资源）；知识库总量从 78 扩至 99 chunks。修复纯中文文件名 source 提取（`_source_name()` 用 hash 兜底）。
- RAG 教训回灌：新增 `xiaoliao_agent/lesson_bridge.py`，从 QualityService 的 approved lessons 中独立检索教训并注入 RAG context；支持词法+向量双模排序，每轮对话自动合并教训来源。
- RAG 源多样化：`KnowledgeBase.context()` 新增 `diverse=True` 参数，保证每个知识源至少有一个代表进入检索结果，防止大源（CBT 73 chunks）淹没小源（资源 7 chunks）。
- RAG 评估：`run_rag_eval.py` 从 `knowledge/rag_regression_v2.json` 统一读取 22 条用例，覆盖 CBT/适老/资源/教训/边界 5 类；输出写入不可变 `rag_runs/<run_id>`。`rag_runs/run-completed` 为 22/22，整体命中率 0.750。
- RAG 监督收尾：006 记忆向量迁移已应用，数据库存在 `ai_memories_embedding_idx`；回填脚本执行 0/0；修复 `MemoryService.backfill_embedding` 缓存失效；新增记忆向量 DB 往返、语义检索、知识库扩展、lesson bridge approved-only 和 RAG 不可变运行测试。全量 209 passed。
- 延迟优化监督收尾：httpx 连接池替换 urllib；KB/lesson/记忆并行检索；`max_tokens` 降至 400、超时 25s；`chat_stream` 改为安全缓冲（Inspector 通过后才发最终回复）；`AgentResult.stage_latencies` 记录 precheck/rag/main/inspector/rewrite/total 分阶段耗时；client 测试改用 httpx MockTransport。全量 204 passed。
- 延迟实测（2026-08-07）：真实模型分阶段计时显示瓶颈是 Qwen Inspector（约 6-12s），主模型约 1.6-6.3s，RAG 约 0.25s；`INSPECTOR_MAX_TOKENS=256` 已启用，160 上限不稳定未采用；记录见 `docs/延迟实测记录.md`。全量 209 passed。
- 知识库向量同步：`import_knowledge.py` 默认纳入适老/地区资源；`knowledge_import.py` 改为使用与运行时一致的 `_source_name`；`backfill_embeddings.py` 只回填缺向量 chunk。数据库 `ai_knowledge_chunks` 已从 78 同步到 99，99/99 有 1536 维向量，来源为 cbt-agent、kb-712886b7、kb-f780bca7、lessons。
- RAG 真实向量评估（2026-08-07）：`python run_rag_eval.py --real --approved-by "用户（本轮批准）"`，`rag_runs/run-real-v1` 22/22，整体词项命中率 0.818；分类 cbt 0.917、适老 0.875、资源 1.000、lesson 0.750、edge 0.000。该数字是 expected_terms 命中率，不等同于人工语义准确率。
- 55 条主链路真实评估（2026-08-07）：`eval_runs/run-real-main-v2` 53/55，一次通过率 96.36%，重写率 0，硬性安全漏放 0，误拦截候选 0；2 例失败为 `AGENT_INVALID_JSON` 和 `AGENT_INVALID_ACTION`。修复医疗边界断言误判并放宽超时到 60s。
- 安全流式输出：新增 `POST /v1/chat/stream` SSE 端点，完整主模型 + Inspector 通过后才流式返回最终回复；支持鉴权、调试权限、限流、幂等和 request_id。OpenAPI、README 和 API 联调示例已同步。

## 外部资源

- 本机 PostgreSQL 18 仍运行在 5432；项目 Docker 数据库使用 55432，避免端口冲突。embedding 使用 Qwen `text-embedding-v4`，Key 仅来自 `.env`。
- 本机 PostgreSQL 18 客户端位于 `C:\Program Files\PostgreSQL\18\bin` 但未加入 PATH；本项目使用 Docker pgvector 数据库，`.env` 已配置本地连接和 Qwen embedding。
- Java 示例已在本机 `javac` 编译，并调用本地 `create_app(fake_agent, api_token="test-token", test_mode=True)` 测试服务返回 HTTP 200；真实 Java 后端地址、Token 和会话摘要仍为“待外部资源”。
- 第 10 步离线确定性评估已在本机完成 55/55；RAG 真实向量评估已执行（`rag_runs/run-real-v1`，22/22）；55 条主链路真实模型评估已执行（`eval_runs/run-real-main-v2`，53/55）；真实 7 天灰度试点为“待执行”。
- 第 11 步本地企微接入契约测试 18/18 通过；真实企微凭据、发送权限、Java 联调地址、热线/值班人和获批测试账号均为“待外部资源”。
- RAG 专项已本地完成并通过监督验收；真实 approved lesson、地区资源热线/值班和真实 embedding 评估仍为“待外部确认”。
