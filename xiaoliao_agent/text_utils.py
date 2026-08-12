"""Unicode-safe text chunking that does not split grapheme clusters."""

import regex as _regex


_GRAPHEME_RE = _regex.compile(r"\X", _regex.V1)


def grapheme_positions(text: str) -> list[int]:
    positions = [0]
    positions.extend(match.end() for match in _GRAPHEME_RE.finditer(text))
    return positions


def grapheme_safe_end(text: str, end: int) -> int:
    """Return the largest grapheme boundary at or before *end*."""
    if end >= len(text):
        return len(text)
    safe = 0
    for position in grapheme_positions(text):
        if position <= end:
            safe = position
        else:
            break
    return safe


def chunk_by_graphemes(text: str, size: int) -> list[str]:
    """Split *text* into chunks of at most *size* grapheme clusters."""
    if not text:
        return []
    if size < 1:
        raise ValueError("size must be >= 1")
    positions = grapheme_positions(text)
    chunks: list[str] = []
    for start_index in range(0, len(positions) - 1, size):
        end_index = min(start_index + size, len(positions) - 1)
        chunks.append(text[positions[start_index]:positions[end_index]])
    return chunks


"""Deterministic enforcement of the ageing-friendly word list."""


AGING_BANNED_PHRASES: dict[str, str] = {
    "你应该振作": "你愿意的话，我们可以一步一步来",
    "你想太多了": "我们可以一起慢慢看这件事",
    "这很简单": "我们可以慢慢试",
    "想开点": "慢慢来，我陪着你",
    "别多想": "我们可以一起慢慢看",
    "你必须": "你可以",
    "显然": "听起来",
}


def apply_aging_filter(text: str) -> str:
    """Replace banned phrases with gentler alternatives."""
    cleaned = text or ""
    for banned in sorted(AGING_BANNED_PHRASES, key=len, reverse=True):
        cleaned = cleaned.replace(banned, AGING_BANNED_PHRASES[banned])
    return cleaned


def contains_banned_phrase(text: str) -> bool:
    return any(banned in (text or "") for banned in AGING_BANNED_PHRASES)
