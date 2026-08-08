# 副 Agent 合同

## 输入

- `user_text`、`intent`、候选回复，以及与主 Agent 相同的 RAG context（最多 6000 字符）。
- RAG 作为不可信参考资料，用于判断候选回复是否遵循知识库，不作为系统指令；RAG 为空时仍按通用安全陪伴原则检查。

## 严格输出

Inspector 必须返回固定九字段 JSON：`crisis_detected`、`safety_violation`、`intent_accurate`、`age_appropriate`、`cbt_appropriate`、`issues`、`suggestion`、`error_pattern`、`lesson`。缺字段、额外字段、错误类型和字符串布尔均视为无效响应。

`error_pattern` 维度固定为：`none`、`crisis`、`medical_boundary`、`unsafe_content`、`intent_mismatch`、`age_inappropriate`、`cbt_inappropriate`、`invalid_response`、`unknown`。模型自造字符串归入 `unknown`，非法类型归入不安全失败。

## 处理终态

- 危机或医疗硬失败：立即使用固定兜底，不发送候选回复。
- Inspector 解析、超时或合同失败：按不安全处理，不发送候选回复。
- 首次软失败：受限 suggestion 只进入一次重写。
- 重写后硬失败：固定兜底。
- 重写后二次软失败：返回版本化保守降级，记录 `AGENT_INSPECTION_FAILED`，不发送未通过回复，不做第三次重写。

## Lesson 审核

Inspector 的非空 lesson 只写入内存待审候选，初始状态为 `pending`。人工可改为 `approved` 或 `rejected`，必须记录 reviewer 和 reason。未审批 lesson 不改变 Prompt；第 8 步前不宣称已持久化。
