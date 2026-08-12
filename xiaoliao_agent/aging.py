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
