"""Parse a Marp Markdown deck: front matter, fenced code, slide splitting.

Single source for the deck's structural rules, so every consumer (engine
detection, embed expansion, the lint's slide numbers) agrees on where the
front matter ends and what is inside a code fence:
- YAML front matter opens and closes on an unindented `---` line
- fences follow the CommonMark closing-length rule (see `iter_fenced`)
- slides split on `---` rules outside fenced code blocks
- HTML comments (Marp directives, presenter notes) are removed
"""

import logging
import re

logger = logging.getLogger(__name__)

_FENCE_RE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
_SEPARATOR_RE = re.compile(r"^---\s*$")
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)


def bespoke_restore_js(hash_value: str) -> str:
    """JS that restores a Bespoke.js slide position after a reload.

    Bespoke only repositions on hashchange, so re-setting the same hash is
    a no-op — flip to slide 0 first, then to the saved hash.
    """
    import json

    return (
        "window.location.hash = '#/0';"
        f"window.location.hash = {json.dumps(hash_value)};"
    )


def _front_matter_end(lines) -> int | None:
    """Index of the closing `---` of a leading front-matter block, else
    None. Works on lines with or without trailing newlines (`\\s*$`)."""
    if not lines or not _SEPARATOR_RE.match(lines[0]):
        return None
    for i in range(1, len(lines)):
        if _SEPARATOR_RE.match(lines[i]):
            return i
    return None


def front_matter_lines(text: str) -> list[str]:
    """The lines inside a leading YAML front-matter block ([] if none)."""
    lines = text.splitlines()
    end = _front_matter_end(lines)
    return lines[1:end] if end is not None else []


def strip_front_matter(text: str) -> str:
    lines = text.splitlines(keepends=True)
    end = _front_matter_end(lines)
    return "".join(lines[end + 1 :]) if end is not None else text


def iter_fenced(lines):
    """Yield (line, in_code) pairs with CommonMark fence tracking.

    `in_code` is True for the fence marker lines themselves and for every
    line inside an open fence. A closing fence must repeat the opening
    character, be at least as long, and carry no info string — an inner
    ``` does not close a ```` block, a tilde fence is not closed by a
    backtick fence, and a "```python" line inside an open ``` block is
    content (CommonMark forbids info strings on closing fences; marp
    keeps the fence open, so we must too).
    """
    fence: str | None = None
    for line in lines:
        m = _FENCE_RE.match(line)
        if m:
            marker = m.group(1)
            if fence is None:
                fence = marker
            elif (
                marker[0] == fence[0]
                and len(marker) >= len(fence)
                and not line[m.end(1) :].strip()
            ):
                fence = None
            yield line, True
            continue
        yield line, fence is not None


def split_slides(text: str) -> list[str]:
    slides: list[str] = []
    current: list[str] = []

    for line, in_code in iter_fenced(text.splitlines()):
        if not in_code and _SEPARATOR_RE.match(line):
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
