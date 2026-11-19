"""The ExCon manual and the participant guide (web/docs/*.md), rendered with Markdown."""

import re
from functools import lru_cache
from pathlib import Path

from django.conf import settings
from django.utils.safestring import mark_safe

DOCS = Path(settings.BASE_DIR) / "docs"
LIST_ITEM = re.compile(r"^(\s*)([-*]|\d+\.)\s")


def _gfm_lists(text):
    """Python-Markdown wants a blank line before a list and 4-space nesting; the docs are
    written GitHub-style (2 spaces). Double every indent and add the blank lines."""
    out, prev = [], ""
    for line in text.splitlines():
        indent = len(line) - len(line.lstrip(" "))
        if indent:
            line = " " * (indent * 2) + line.lstrip(" ")
        if LIST_ITEM.match(line) and not indent and prev.strip() and not LIST_ITEM.match(prev):
            out.append("")
        out.append(line)
        prev = line
    return "\n".join(out)


@lru_cache(maxsize=4)
def _render(name, mtime):
    import markdown
    text = (DOCS / name).read_text(encoding="utf-8")
    return markdown.markdown(_gfm_lists(text), extensions=["tables", "fenced_code", "toc"])


def html(name):
    path = DOCS / name
    if not path.exists():
        return mark_safe("<p>Not found.</p>")
    return mark_safe(_render(name, path.stat().st_mtime))
