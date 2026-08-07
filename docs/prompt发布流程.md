# Prompt 发布流程

1. **提出**：新建不可变的语义版本内容文件，补充 `prompt_versions/registry.json` 的 owner、scope、changelog、创建时间和 rollback_to；不覆盖旧文件。
2. **测试**：运行 Prompt 静态检查、结构化 Schema 测试和 `prompt_regression_v1.json` 高风险回归。人工标签只能使用 `pass`、`fail`、`needs_review`，不能由模型自评充当人工通过。
3. **比较**：使用 `python compare_prompts.py main-agent 0.9.0 1.0.0` 输出版本差异，并人工审查禁用表达、行动白名单、注入边界和安全话术变化。
4. **审批**：由 Prompt owner 和安全负责人确认测试报告、差异和回滚版本；未审批版本保持不可选。
5. **发布**：在 `.env` 中显式设置 `PROMPT_VERSION`，启动时解析不到版本则拒绝启动，不静默回退到未知版本。
6. **监控**：记录 Prompt 版本、重写率、硬拦截率、非法 JSON 和高风险回归结果；本轮不建设日志平台。
7. **回滚**：把 `PROMPT_VERSION` 改为 registry 中的 `rollback_to` 版本，重新运行全套测试和高风险回归后再启动服务。

当前默认版本为 `1.2.0`，回滚点为 `1.1.0`；旧版本继续保留各自回滚点。文件型 registry 可在后续迁移到数据库，但旧版本内容不得覆盖。
