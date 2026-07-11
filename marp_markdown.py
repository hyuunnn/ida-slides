"""Parse a Marp Markdown deck into per-slide HTML fragments.

Handles the Marp conventions that matter for in-IDA rendering:
- YAML front matter is stripped (theme/paginate directives don't apply here)
- slides are split on `---` rules outside fenced code blocks
- HTML comments (Marp directives, presenter notes) are removed
"""

import logging
import re

logger = logging.getLogger(__name__)

try:
    import markdown as _markdown_mod
except ImportError:  # gate at call site so the plugin still loads without it
    _markdown_mod = None

_FENCE_RE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
_SEPARATOR_RE = re.compile(r"^---\s*$")
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)

_MD_EXTENSIONS = ["fenced_code", "tables", "sane_lists"]


def bespoke_restore_js(hash_value: str) -> str:
    """JS that restores a Bespoke.js slide position after a reload.

    Bespoke only repositions on hashchange, so re-setting the same hash is
    a no-op — flip to slide 0 first, then to the saved hash. Shared by the
    WKWebView and QtWebEngine renderers.
    """
    import json

    return (
        "window.location.hash = '#/0';"
        f"window.location.hash = {json.dumps(hash_value)};"
    )


def strip_front_matter(text: str) -> str:
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return text
    for i in range(1, len(lines)):
        if _SEPARATOR_RE.match(lines[i]):
            return "".join(lines[i + 1 :])
    return text


def split_slides(text: str) -> list[str]:
    slides: list[str] = []
    current: list[str] = []
    fence: str | None = None

    for line in text.splitlines():
        fence_match = _FENCE_RE.match(line)
        if fence_match:
            marker = fence_match.group(1)
            if fence is None:
                fence = marker
            elif marker[0] == fence[0] and len(marker) >= len(fence):
                # CommonMark: a closing fence must use the same char and
                # be at least as long as the opener — an inner ``` does
                # not close a ```` block
                fence = None
            current.append(line)
            continue

        if fence is None and _SEPARATOR_RE.match(line):
            # CommonMark: '---' directly under a non-blank line is a setext
            # H2 underline, not a thematic break — marp keeps it on the
            # same slide, so we must too
            if current and current[-1].strip():
                current.append(line)
                continue
            slides.append("\n".join(current).strip())
            current = []
            continue

        current.append(line)

    slides.append("\n".join(current).strip())
    return [s for s in slides if s] or [""]


def parse_deck(text: str) -> list[str]:
    """Full deck text -> list of per-slide Markdown sources."""
    body = strip_front_matter(text)
    body = _COMMENT_RE.sub("", body)
    return split_slides(body)


def slide_to_html(md_source: str) -> str:
    if _markdown_mod is None:
        raise RuntimeError(
            "the 'markdown' package is required to render .md decks "
            "(pip install markdown)"
        )
    return _markdown_mod.markdown(md_source, extensions=_MD_EXTENSIONS)
