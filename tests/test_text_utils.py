from xiaoliao_agent.text_utils import chunk_by_graphemes, grapheme_safe_end


def test_chunk_by_graphemes_keeps_family_emoji_whole():
    text = "你好👨‍👩‍👧世界"
    chunks = chunk_by_graphemes(text, 4)
    assert "".join(chunks) == text
    assert any("👨‍👩‍👧" in chunk for chunk in chunks)
    assert not any(chunk in {"👨", "👩", "👧"} for chunk in chunks)


def test_chunk_by_graphemes_respects_size():
    text = "一二三四五六"
    assert chunk_by_graphemes(text, 4) == ["一二三四", "五六"]


def test_grapheme_safe_end_does_not_split_zwj_emoji():
    text = "你👨‍👩‍👧好"
    end = grapheme_safe_end(text, 2)
    assert text[:end].endswith("你")
    assert text[end] == "👨"
