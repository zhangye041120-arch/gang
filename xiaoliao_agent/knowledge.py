from dataclasses import dataclass
import hashlib
import re
from pathlib import Path
from typing import Callable, Iterable


VectorSearch = Callable[[str, int], list[tuple[str, float]]]


@dataclass(frozen=True)
class Chunk:
    chunk_id: str
    heading: str
    content: str
    source: str = "cbt"
    version: str = "v1"
    heading_path: tuple[str, ...] = ()
    content_hash: str = ""


def _tokens(text: str) -> set[str]:
    result: set[str] = set()
    for match in re.findall(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]+", text.lower()):
        if re.fullmatch(r"[\u4e00-\u9fff]+", match):
            result.update(match)
            result.update(match[i : i + 2] for i in range(len(match) - 1))
        else:
            result.add(match)
    return result


def _source_name(stem: str) -> str:
    """Derive a stable ASCII source slug from a file stem (which may be pure CJK)."""
    ascii_part = re.sub(r"[^A-Za-z0-9]+", "-", stem).strip("-").lower()
    if ascii_part:
        return ascii_part
    # Pure-CJK filenames: hash the stem to get a stable, readable slug
    digest = hashlib.sha256(stem.encode("utf-8")).hexdigest()[:8]
    return f"kb-{digest}"


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _stable_chunk_id(source: str, version: str, heading_path: tuple[str, ...], content: str) -> str:
    identity = "\x1f".join((source, version, "/".join(heading_path), _content_hash(content)))
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]
    return f"{source}-{version}-{digest}"


def split_markdown(
    text: str,
    max_chars: int = 1800,
    overlap: int = 180,
    *,
    source: str = "cbt",
    version: str = "v1",
) -> list[Chunk]:
    lines = text.splitlines()
    sections: list[tuple[tuple[str, ...], list[str]]] = []
    path_by_level: dict[int, str] = {}
    body: list[str] = []

    def flush() -> None:
        if body:
            sections.append((tuple(path_by_level[level] for level in sorted(path_by_level)), body.copy()))
            body.clear()

    for line in lines:
        match = re.match(r"^(#{2,4})\s+(.+?)\s*$", line)
        if match:
            flush()
            level = len(match.group(1))
            path_by_level = {key: value for key, value in path_by_level.items() if key < level}
            path_by_level[level] = match.group(2).strip()
        else:
            body.append(line)
    flush()

    chunks: list[Chunk] = []
    for heading_path, section_lines in sections:
        raw = "\n".join(section_lines).strip()
        if not raw:
            continue
        heading = heading_path[-1] if heading_path else "知识库"
        start = 0
        while start < len(raw):
            end = min(len(raw), start + max_chars)
            piece = raw[start:end].strip()
            if piece:
                chunks.append(Chunk(
                    chunk_id=_stable_chunk_id(source, version, heading_path, piece),
                    heading=heading,
                    content=piece,
                    source=source,
                    version=version,
                    heading_path=heading_path,
                    content_hash=_content_hash(piece),
                ))
            if end >= len(raw):
                break
            start = max(0, end - overlap)
    return chunks


def _normalise_scores(scores: dict[str, float]) -> dict[str, float]:
    if not scores:
        return {}
    minimum = min(scores.values())
    maximum = max(scores.values())
    if maximum == minimum:
        return {key: 1.0 for key in scores}
    return {key: (value - minimum) / (maximum - minimum) for key, value in scores.items()}


class KnowledgeBase:
    def __init__(self, chunks: list[Chunk], vector_search: VectorSearch | None = None):
        self.chunks = chunks
        self.vector_search = vector_search
        self.last_search_error: dict[str, str] | None = None
        self._token_cache = {
            chunk.chunk_id: _tokens(chunk.heading + "\n" + chunk.content)
            for chunk in chunks
        }

    @classmethod
    def from_files(
        cls,
        *paths: Path,
        version: str = "v1",
        vector_search: VectorSearch | None = None,
    ) -> "KnowledgeBase":
        chunks: list[Chunk] = []
        for path in paths:
            if not path.exists():
                continue
            source = _source_name(path.stem)
            chunks.extend(split_markdown(
                path.read_text(encoding="utf-8"),
                source=source,
                version=version,
            ))
        return cls(chunks, vector_search=vector_search)

    def _lexical_scores(self, query: str, allowed_sources: set[str] | None, version: str | None) -> dict[str, float]:
        query_tokens = _tokens(query)
        if not query_tokens:
            return {}
        scores: dict[str, float] = {}
        for chunk in self.chunks:
            if allowed_sources is not None and chunk.source not in allowed_sources:
                continue
            if version is not None and chunk.version != version:
                continue
            overlap = len(query_tokens & self._token_cache[chunk.chunk_id])
            if not overlap:
                continue
            score = overlap / max(1, len(query_tokens))
            if any(term in chunk.heading for term in re.findall(r"[\u4e00-\u9fff]{2,}", query)):
                score += 0.12
            scores[chunk.chunk_id] = score
        return scores

    def search(
        self,
        query: str,
        top_k: int = 3,
        *,
        allowed_sources: Iterable[str] | None = None,
        version: str | None = None,
    ) -> list[tuple[Chunk, float]]:
        self.last_search_error = None
        source_filter = set(allowed_sources) if allowed_sources is not None else None
        lexical_scores = self._lexical_scores(query, source_filter, version)
        vector_scores: dict[str, float] = {}
        if self.vector_search is not None:
            try:
                for chunk_id, score in self.vector_search(query, max(top_k * 4, top_k)):
                    if any(chunk.chunk_id == chunk_id and (source_filter is None or chunk.source in source_filter)
                           and (version is None or chunk.version == version) for chunk in self.chunks):
                        vector_scores[chunk_id] = float(score)
            except Exception:
                self.last_search_error = {
                    "type": "vector_search_unavailable",
                    "message": "向量检索不可用，已回退词法检索",
                }

        lexical_norm = _normalise_scores(lexical_scores)
        vector_norm = _normalise_scores(vector_scores)
        ids = set(lexical_norm) | set(vector_norm)
        chunks_by_id = {chunk.chunk_id: chunk for chunk in self.chunks}
        ranked = []
        for chunk_id in ids:
            score = 0.6 * lexical_norm.get(chunk_id, 0.0) + 0.4 * vector_norm.get(chunk_id, 0.0)
            ranked.append((score, chunks_by_id[chunk_id]))
        ranked.sort(key=lambda item: (-item[0], item[1].chunk_id))
        return [(chunk, round(score, 4)) for score, chunk in ranked[:top_k]]

    @property
    def _source_names(self) -> list[str]:
        seen: dict[str, bool] = {}
        result: list[str] = []
        for chunk in self.chunks:
            if chunk.source not in seen:
                seen[chunk.source] = True
                result.append(chunk.source)
        return result

    def context(
        self,
        query: str,
        top_k: int = 3,
        *,
        allowed_sources: Iterable[str] | None = None,
        version: str | None = None,
        diverse: bool = True,
    ) -> tuple[str, list[dict[str, object]]]:
        if not diverse or (allowed_sources is not None):
            results = self.search(query, top_k=top_k, allowed_sources=allowed_sources, version=version)
        else:
            # Source-diverse: reserve one slot per source, then fill by score.
            # Prevents large knowledge bases from crowding out smaller ones.
            global_results = self.search(query, top_k=top_k * 3, version=version)
            seen_ids: set[str] = set()
            diverse: list[tuple[Chunk, float]] = []
            sources_represented: set[str] = set()
            # First pass — one per source
            for chunk, score in global_results:
                if len(diverse) >= top_k:
                    break
                if chunk.source not in sources_represented:
                    sources_represented.add(chunk.source)
                    seen_ids.add(chunk.chunk_id)
                    diverse.append((chunk, score))
            # Second pass — fill remaining by score
            for chunk, score in global_results:
                if len(diverse) >= top_k:
                    break
                if chunk.chunk_id not in seen_ids:
                    seen_ids.add(chunk.chunk_id)
                    diverse.append((chunk, score))
            diverse.sort(key=lambda item: -item[1])
            results = diverse

        sources: list[dict[str, object]] = []
        blocks: list[str] = []
        for chunk, score in results:
            sources.append({
                "chunk_id": chunk.chunk_id,
                "source": chunk.source,
                "version": chunk.version,
                "heading_path": list(chunk.heading_path),
                "heading": chunk.heading,
                "score": score,
            })
            blocks.append(f"[知识来源 {chunk.source} | {chunk.heading}]\n{chunk.content}")
        return "\n\n".join(blocks), sources
