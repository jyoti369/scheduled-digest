"""Small string helpers."""
import re


def slugify(value, max_len=80):
    """Lowercase, hyphenate, strip to a url-safe slug."""
    s = re.sub(r"[^a-z0-9-]+", "-", (value or "").lower()).strip("-")
    return s[:max_len]


def truncate(value, limit=200, suffix="..."):
    """Clip a string to limit chars, adding suffix when clipped."""
    value = value or ""
    if len(value) <= limit:
        return value
    return value[: max(0, limit - len(suffix))] + suffix


def collapse_whitespace(value):
    """Collapse runs of whitespace into single spaces."""
    return re.sub(r"\s+", " ", value or "").strip()
