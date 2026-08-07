# CBT 知识库生产化说明

## 当前实现

- Markdown 按二级、三级、四级标题切块，保留 `heading_path`。
- `chunk_id` 由 `source`、`version`、标题路径和内容 SHA-256 稳定生成；相同内容重复导入可跳过。
- 本地词法检索是强制降级路径。可选向量检索通过回调接入，分数先分别归一化，再按词法 0.6、向量 0.4 融合。
- 检索结果包含 `chunk_id`、`source`、`version`、`heading_path`、`heading` 和 `score`。

## 配置

复制 `.env.example` 的知识配置到本地 `.env`，填写 `KNOWLEDGE_DATABASE_URL`、`EMBEDDING_PROVIDER`、`EMBEDDING_MODEL` 和 `EMBEDDING_DIMENSION`。密码只保存在 `.env`，不提交示例文件。

`migrations/001_ai_knowledge_chunks.sql` 当前固定 `vector(1536)`，必须与 `EMBEDDING_DIMENSION` 一致；切换维度应创建新迁移和新知识版本，不直接改历史列。

## Docker 本地数据库

项目提供 `docker-compose.yml`，使用 `pgvector/pgvector:pg18`，宿主机端口为 `55432`，不会占用本机 PostgreSQL 的 `5432`：

```text
docker compose up -d knowledge-db
Get-Content migrations/001_ai_knowledge_chunks.sql | docker exec -i xiaoliao-knowledge-db psql -U xiaoliao -d xiaoliao -v ON_ERROR_STOP=1
python import_knowledge.py
```

`.env.docker` 仅用于本机开发并已加入 `.gitignore`。容器绑定到 `127.0.0.1`，不应直接暴露到局域网。

## 导入与版本切换

```text
python import_knowledge.py
python import_knowledge.py knowledge/CBT知识库_Agent版.md knowledge/lessons.md
```

设置 `KNOWLEDGE_VERSION` 后重新导入会生成新版本的稳定 ID。PostgreSQL 导入使用唯一约束避免重复写入；没有数据库 URL 时命令只运行内存 dry-run，不代表已经持久化。

## 故障回退

数据库或 embedding 服务不可用时，调用方应保留本地 `KnowledgeBase`，检索自动回退到词法结果；没有命中时返回空来源和空 context，不编造引用。向量故障会保留结构化 `last_search_error`，不把供应商错误体写入用户响应。

## 当前生产差距

Docker PostgreSQL 18 与 pgvector 0.8.6 已在本机 `127.0.0.1:55432` 验证，迁移、78 个 chunk 导入和 Qwen `text-embedding-v4` 的 1536 维向量回填已完成。运行时先融合向量与词法分数，embedding 服务故障时回退词法。
