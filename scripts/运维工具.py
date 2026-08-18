"""统一运维工具入口。

原先分散在项目根目录的小工具合并为一个入口，功能不变：

- configure / token / check：模型与 API 配置
- backfill-knowledge / backfill-memory：向量回填
- compare-prompts / sync-prompts：Prompt 版本工具
- quality-review / metrics-dashboard：质量与评估看板
"""

import argparse
import difflib
import hashlib
import json
import re
import secrets
import sys
from getpass import getpass
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from xiaoliao_agent.config import Settings
from xiaoliao_agent.knowledge import KnowledgeBase
from xiaoliao_agent.memory import PostgresMemoryRepository
from xiaoliao_agent.prompts import PROMPT_ROOT, get_prompt_spec
from xiaoliao_agent.providers import (
    EmbeddingError,
    OpenAICompatibleClient,
    OpenAICompatibleEmbeddingClient,
)

ENV_PATH = ROOT / ".env"


def _ask(label: str, default: str) -> str:
    value = input(f"{label} [{default}]：").strip()
    return value or default


def cmd_configure() -> int:
    print("小辽智能体 API 配置")
    print("密钥只写入当前项目的 .env，不会显示在屏幕上，也不会写入 .env.example。")
    print()

    deepseek_base_url = _ask("DeepSeek Base URL", "https://api.deepseek.com/v1")
    deepseek_model = _ask("DeepSeek 模型 ID", "deepseek-v4-flash")
    deepseek_key = getpass("DeepSeek API Key：").strip()

    qwen_base_url = _ask("Qwen Base URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
    qwen_model = _ask("Qwen 模型 ID", "qwen3.7-max")
    qwen_key = getpass("Qwen API Key：").strip()

    if not deepseek_key or not qwen_key:
        print("配置未保存：两个 API Key 都不能为空。")
        return 2

    content = f"""DEEPSEEK_BASE_URL={deepseek_base_url}
DEEPSEEK_API_KEY={deepseek_key}
DEEPSEEK_MODEL={deepseek_model}

QWEN_BASE_URL={qwen_base_url}
QWEN_API_KEY={qwen_key}
QWEN_MODEL={qwen_model}

AGENT_TEMPERATURE=0.4
AGENT_MAX_TOKENS=800
INSPECTOR_MAX_TOKENS=256
INSPECTOR_ENABLE_THINKING=false
AGENT_TIMEOUT_SECONDS=45
AGENT_TOP_K=3
"""
    temp_path = ENV_PATH.with_suffix(".env.tmp")
    temp_path.write_text(content, encoding="utf-8")
    temp_path.replace(ENV_PATH)
    print(f"配置已保存：{ENV_PATH}")
    print("下一步运行“检查API连接.bat”。")
    return 0


def cmd_token() -> int:
    content = ENV_PATH.read_text(encoding="utf-8") if ENV_PATH.exists() else ""
    match = re.search(r"^API_TOKEN=(.*)$", content, flags=re.M)
    if match and match.group(1).strip():
        print("API_TOKEN 已存在，未覆盖。")
        return 0
    line = "API_TOKEN=" + secrets.token_urlsafe(48)
    if match:
        content = content[: match.start()] + line + content[match.end() :]
    else:
        content = content.rstrip() + "\n" + line + "\n"
    ENV_PATH.write_text(content, encoding="utf-8")
    print("API_TOKEN 已生成并写入 .env，未显示其值。")
    return 0


def _check_client(name: str, client: OpenAICompatibleClient) -> bool:
    print(f"正在检查 {name} ...")
    try:
        reply = client.chat([
            {"role": "system", "content": "这是连接测试。"},
            {"role": "user", "content": "请只回复 OK"},
        ])
    except Exception as exc:
        print(f"[失败] {name}: {exc}")
        return False
    print(f"[成功] {name}: {reply[:120]}")
    return True


def cmd_check() -> int:
    settings = Settings.from_env()
    try:
        settings.validate_live()
    except RuntimeError as exc:
        print(exc)
        print("请先运行“配置API.bat”。")
        return 2

    deepseek = OpenAICompatibleClient(
        settings.deepseek_base_url,
        settings.deepseek_api_key,
        settings.deepseek_model,
        settings,
    )
    qwen = OpenAICompatibleClient(
        settings.qwen_base_url,
        settings.qwen_api_key,
        settings.qwen_model,
        settings,
    )
    deepseek_ok = _check_client(f"DeepSeek ({settings.deepseek_model})", deepseek)
    qwen_ok = _check_client(f"Qwen ({settings.qwen_model})", qwen)
    if deepseek_ok and qwen_ok:
        print("两个真实模型接口都已连接成功。现在可以运行“开始真实模型对话.bat”。")
        return 0
    print("至少一个接口连接失败。请检查 Key、Base URL、模型 ID、余额和地域。")
    return 1


def cmd_backfill_knowledge(args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    if settings.embedding_provider == "none" or not settings.embedding_model:
        print("embedding 未配置：请设置 EMBEDDING_PROVIDER 和 EMBEDDING_MODEL")
        return 2
    if not settings.knowledge_database_url:
        print("KNOWLEDGE_DATABASE_URL 未配置")
        return 2
    if settings.embedding_provider.lower() != "qwen":
        print("当前只实现 qwen OpenAI 兼容 embedding 提供商")
        return 2

    kb = KnowledgeBase.from_files(
        settings.knowledge_path,
        settings.lessons_path,
        version=settings.knowledge_version,
    )
    chunks_by_id = {chunk.chunk_id: chunk for chunk in kb.chunks}
    with psycopg.connect(settings.knowledge_database_url) as connection:
        rows = connection.execute(
            "SELECT id FROM ai_knowledge_chunks WHERE embedding IS NULL AND version=%s",
            (settings.knowledge_version,),
        ).fetchall()
        missing_ids = [str(row[0]) for row in rows]
    print(f"知识块总数={len(kb.chunks)}，缺向量={len(missing_ids)}")
    if not missing_ids:
        print("所有知识块均已 embedding，无需回填。")
        return 0

    client = OpenAICompatibleEmbeddingClient(
        settings.qwen_base_url,
        settings.qwen_api_key,
        settings.embedding_model,
        dimension=settings.embedding_dimension,
        timeout=settings.timeout_seconds,
    )
    missing_chunks = [chunks_by_id[chunk_id] for chunk_id in missing_ids if chunk_id in chunks_by_id]
    if len(missing_chunks) != len(missing_ids):
        print(f"警告：{len(missing_ids) - len(missing_chunks)} 个缺向量 id 不在当前知识文件里")
    vectors: list[list[float]] = []
    try:
        for start in range(0, len(missing_chunks), args.batch_size):
            batch = missing_chunks[start : start + args.batch_size]
            vectors.extend(client.embed([chunk.heading + "\n" + chunk.content for chunk in batch]))
            print(f"embedded={len(vectors)}/{len(missing_chunks)}")
    except EmbeddingError as exc:
        print(f"embedding 失败：{exc}")
        return 1

    with psycopg.connect(settings.knowledge_database_url) as connection:
        for chunk, vector in zip(missing_chunks, vectors):
            connection.execute(
                "UPDATE ai_knowledge_chunks SET embedding=%s::vector WHERE id=%s AND version=%s",
                (json.dumps(vector, separators=(",", ":")), chunk.chunk_id, chunk.version),
            )
    print(f"backfilled={len(vectors)} dimension={settings.embedding_dimension}")
    return 0


def cmd_backfill_memory(args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    if settings.embedding_provider == "none" or not settings.embedding_model:
        print("embedding 未配置：请设置 EMBEDDING_PROVIDER 和 EMBEDDING_MODEL")
        return 2
    if not settings.knowledge_database_url:
        print("KNOWLEDGE_DATABASE_URL 未配置")
        return 2

    repo = PostgresMemoryRepository(settings.knowledge_database_url)
    with_emb, without_emb = repo.count_embeddings()
    print(f"当前状态: with_embedding={with_emb}, without_embedding={without_emb}")

    client = OpenAICompatibleEmbeddingClient(
        settings.qwen_base_url,
        settings.qwen_api_key,
        settings.embedding_model,
        dimension=settings.embedding_dimension,
        timeout=settings.timeout_seconds,
    )

    missing_ids = repo.find_embeddings_missing(limit=args.limit or 10_000)
    if not missing_ids:
        print("所有活跃记忆均已embedding，无需回填。")
        return 0

    print(f"需要回填 {len(missing_ids)} 条记忆")

    mem_contents: dict[str, str] = {}
    with psycopg.connect(settings.knowledge_database_url, connect_timeout=5) as connection:
        for start in range(0, len(missing_ids), 500):
            batch = missing_ids[start : start + 500]
            rows = connection.execute(
                "SELECT memory_id, content FROM ai_memories WHERE memory_id = ANY(%s) AND deleted_at IS NULL",
                (batch,),
            ).fetchall()
            for row in rows:
                mem_contents[row[0]] = row[1]

    print(f"获取到 {len(mem_contents)} 条内容")

    count = 0
    ids_list = list(mem_contents.keys())
    try:
        for start in range(0, len(ids_list), args.batch_size):
            batch_ids = ids_list[start : start + args.batch_size]
            texts = [mem_contents[mid] for mid in batch_ids]
            vectors = client.embed(texts)
            for mid, vector in zip(batch_ids, vectors):
                repo.update_embedding(mid, vector)
            count += len(vectors)
            print(f"backfilled={count}/{len(ids_list)}")
    except EmbeddingError as exc:
        print(f"embedding 失败：{exc}")
        return 1

    print(f"完成: backfilled={count} dimension={settings.embedding_dimension}")
    return 0


def cmd_compare_prompts(args: argparse.Namespace) -> int:
    old = get_prompt_spec(args.prompt_id, args.old_version)
    new = get_prompt_spec(args.prompt_id, args.new_version)
    print(f"{old.prompt_id}: {old.semantic_version} -> {new.semantic_version}")
    print(f"old_changelog={old.changelog}")
    print(f"new_changelog={new.changelog}")
    for line in difflib.unified_diff(
        old.content.splitlines(),
        new.content.splitlines(),
        fromfile=f"{args.prompt_id}@{args.old_version}",
        tofile=f"{args.prompt_id}@{args.new_version}",
        lineterm="",
    ):
        print(line)
    return 0


def cmd_quality_review() -> int:
    settings = Settings.from_env()
    with psycopg.connect(settings.knowledge_database_url) as connection:
        patterns = connection.execute(
            "SELECT error_pattern, count(*) FROM ai_inspection_logs GROUP BY error_pattern ORDER BY count(*) DESC"
        ).fetchall()
        pending_lessons = connection.execute("SELECT count(*) FROM ai_lessons WHERE status='pending'").fetchone()[0]
        pending_patches = connection.execute("SELECT count(*) FROM ai_prompt_patches WHERE status='pending'").fetchone()[0]
        crisis_index = connection.execute(
            "SELECT event_id, evidence_code, route, status, created_at FROM ai_crisis_events ORDER BY created_at DESC LIMIT 20"
        ).fetchall()
    print("error_patterns")
    for pattern, count in patterns:
        print(f"{pattern}: {count}")
    print(f"pending_lessons={pending_lessons}")
    print(f"pending_patches={pending_patches}")
    print(f"crisis_case_index={len(crisis_index)}")
    for event_id, evidence_code, route, status, created_at in crisis_index:
        print(f"{event_id}|{evidence_code}|{route}|{status}|{created_at.isoformat()}")
    return 0


def cmd_metrics_dashboard(args: argparse.Namespace) -> int:
    root = Path(args.out)
    if not root.exists():
        print("尚无评估运行目录")
        return 0
    rows = []
    for run_dir in sorted(root.glob("run-*")):
        manifest_path = run_dir / "manifest.json"
        metrics_path = run_dir / "metrics.json"
        if not manifest_path.exists() or not metrics_path.exists():
            continue
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
        rows.append({
            "run_id": run_dir.name,
            "mode": manifest.get("mode"),
            "pass_rate": metrics.get("pass_rate"),
            "hard_safety_misses": metrics.get("hard_safety_misses"),
            "p50_latency_ms": metrics.get("p50_latency_ms"),
            "p95_latency_ms": metrics.get("p95_latency_ms"),
            "total_tokens": metrics.get("total_tokens"),
            "cost_total": metrics.get("cost_total"),
            "cost_complete": metrics.get("cost_complete"),
        })
    print(json.dumps(rows, ensure_ascii=False, indent=2))
    return 0


def cmd_sync_prompts() -> int:
    settings = Settings.from_env()
    registry = json.loads((PROMPT_ROOT / "registry.json").read_text(encoding="utf-8"))
    with psycopg.connect(settings.knowledge_database_url) as connection:
        for item in registry["prompts"]:
            content = (PROMPT_ROOT / item["content_file"]).read_text(encoding="utf-8")
            digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
            connection.execute(
                """
                INSERT INTO ai_prompt_versions
                    (prompt_id, semantic_version, content_hash, owner, changelog, created_at)
                VALUES (%s,%s,%s,%s,%s,%s)
                ON CONFLICT (prompt_id, semantic_version) DO NOTHING
                """,
                (item["prompt_id"], item["semantic_version"], digest, item["owner"], item["changelog"], item["created_at"]),
            )
            stored = connection.execute(
                "SELECT content_hash FROM ai_prompt_versions WHERE prompt_id=%s AND semantic_version=%s",
                (item["prompt_id"], item["semantic_version"]),
            ).fetchone()[0]
            if stored != digest:
                raise RuntimeError(f"不可变 Prompt 版本哈希冲突：{item['prompt_id']}@{item['semantic_version']}")
    print(f"synced={len(registry['prompts'])}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="小辽智能体运维工具")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("configure", help="配置模型 API Key")
    sub.add_parser("token", help="生成或保留 API_TOKEN")
    sub.add_parser("check", help="检查两个真实模型接口连接")

    backfill_knowledge = sub.add_parser("backfill-knowledge", help="回填 CBT 知识向量")
    backfill_knowledge.add_argument("--batch-size", type=int, default=8)

    backfill_memory = sub.add_parser("backfill-memory", help="回填用户记忆向量")
    backfill_memory.add_argument("--batch-size", type=int, default=8)
    backfill_memory.add_argument("--limit", type=int, default=0, help="上限（0=全部缺失行）")

    compare = sub.add_parser("compare-prompts", help="比较两个不可变 Prompt 版本")
    compare.add_argument("prompt_id", choices=["main-agent", "inspector", "rewrite"])
    compare.add_argument("old_version")
    compare.add_argument("new_version")

    sub.add_parser("quality-review", help="查看质量日志与待审项目")
    metrics = sub.add_parser("metrics-dashboard", help="查看不可变评估运行摘要")
    metrics.add_argument("--out", default="eval_runs")
    sub.add_parser("sync-prompts", help="校验并同步 Prompt 版本哈希")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.command == "configure":
        return cmd_configure()
    if args.command == "token":
        return cmd_token()
    if args.command == "check":
        return cmd_check()
    if args.command == "backfill-knowledge":
        return cmd_backfill_knowledge(args)
    if args.command == "backfill-memory":
        return cmd_backfill_memory(args)
    if args.command == "compare-prompts":
        return cmd_compare_prompts(args)
    if args.command == "quality-review":
        return cmd_quality_review()
    if args.command == "metrics-dashboard":
        return cmd_metrics_dashboard(args)
    if args.command == "sync-prompts":
        return cmd_sync_prompts()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
