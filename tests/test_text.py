from src.utils.text import slugify, truncate, collapse_whitespace


def test_slugify_basic():
    assert slugify("Hello World!") == "hello-world"


def test_slugify_max_len():
    assert slugify("a" * 200, max_len=10) == "a" * 10


def test_truncate_short_passthrough():
    assert truncate("short", limit=10) == "short"


def test_truncate_clips():
    assert truncate("abcdefghij", limit=5) == "ab..."


def test_collapse_whitespace():
    assert collapse_whitespace("  a   b\n c ") == "a b c"
