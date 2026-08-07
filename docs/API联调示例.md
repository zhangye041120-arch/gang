# 小辽 M7 API 联调示例

端口唯一来源是 `API_PORT`，默认 `8081`。以下示例假设服务地址为 `http://127.0.0.1:8081`，Token 已写入环境变量 `XIAOLIAO_API_TOKEN`。

## curl

普通请求：

```bash
curl -sS http://127.0.0.1:8081/v1/chat \
  -H "Authorization: Bearer $XIAOLIAO_API_TOKEN" \
  -H "Content-Type: application/json; charset=utf-8" \
  -H "X-Request-ID: curl-demo-001" \
  -H "Idempotency-Key: curl-demo-001" \
  -d '{
    "user_id": "user_001",
    "session_id": "session_001",
    "message": "我最近什么都不想做",
    "context": {
      "consent": {"personalization": false},
      "user_summary": ""
    },
    "debug": false
  }'
```

响应头里的 `X-Request-ID` 用于日志追踪；请求失败时响应体里也会返回同一个 `request_id`。

超时重试应复用同一个 `Idempotency-Key`，避免重复写入危机事件、行动推荐或质量日志：

```bash
curl -sS -w "\nHTTP %{http_code}\n" http://127.0.0.1:8081/v1/chat \
  -H "Authorization: Bearer $XIAOLIAO_API_TOKEN" \
  -H "Content-Type: application/json; charset=utf-8" \
  -H "Idempotency-Key: retry-safe-001" \
  -d '{"user_id":"user_001","session_id":"session_001","message":"我最近有点累","context":{"consent":{"personalization":false},"user_summary":""},"debug":false}'
```

## Java 11+

编译：

```bash
javac -encoding UTF-8 -d examples/java/build examples/java/V1ChatClient.java
```

运行：

```bash
java -cp examples/java/build V1ChatClient http://127.0.0.1:8081 "$XIAOLIAO_API_TOKEN"
```

示例会打印 HTTP 状态、`X-Request-ID` 和响应正文。真实 Java 联调需要 Java 后端服务地址、Token 和生产会话摘要；未完成外部联调前，本项目进度统一记为“待外部资源”。

## 安全流式输出

`POST /v1/chat/stream` 返回 SSE，只有在主模型和 Inspector 全部通过后才流式输出最终回复：

```bash
curl -N -sS http://127.0.0.1:8081/v1/chat/stream \
  -H "Authorization: Bearer $XIAOLIAO_API_TOKEN" \
  -H "Content-Type: application/json; charset=utf-8" \
  -H "X-Request-ID: stream-demo-001" \
  -H "Idempotency-Key: stream-demo-001" \
  -d '{"user_id":"user_001","session_id":"session_001","message":"我最近有点累","context":{"consent":{"personalization":false},"user_summary":""},"debug":false}'
```

SSE 事件为 `message`（回复片段）和 `done`（最终合同字段）。该端点不会把未过审的模型原始 token 推给用户。
