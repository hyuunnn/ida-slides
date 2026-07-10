"""Markdown preprocessing: expand `@name[a:b]` tokens into fenced code
blocks holding the Hex-Rays pseudocode of that function.

Runs before the deck reaches the rendering engine (marp CLI, slidev, or the
built-in viewer), so embeds work identically everywhere. Supported forms:

    @sub_401000[1:5]     lines 1-5 of the pseudocode
    @sub_401000[:5]      lines 1-5
    @sub_401000[3:]      line 3 to the end
    @sub_401000[7]       line 7 only
    @sub_401000[]        the whole function
    @sub_401000[1:8@5]   lines 1-8 with line 5 marked (►)
    @!sub_401000[1:8@5]  same, plus a @!name:5 presenter-follow caption so
                         IDA auto-jumps there when the slide is shown

Tokens inside fenced code blocks or inline backtick spans are left alone so
decks can document the syntax itself.
"""

import logging
import re

import ida_links

logger = logging.getLogger(__name__)

_FENCE_RE = re.compile(r"^\s{0,3}(```|~~~)")
_INLINE_CODE_RE = re.compile(r"`[^`]*`")

EMBED_RE = re.compile(
    rf"@(!?)({ida_links._NAME_PATTERN})\[(\d*)(?::(\d*))?(?:@(\d+))?\]"
)


def decompile_lines(
    name: str, start: int | None = None, end: int | None = None
) -> tuple[list[str] | None, str | None]:
    """Return (pseudocode lines, None) or (None, error message).

    `start`/`end` are 1-indexed and inclusive.
    """
    import ida_funcs
    import ida_idaapi
    import ida_lines

    try:
        import ida_hexrays
    except ImportError:
        return None, "Hex-Rays decompiler not available"

    ea = ida_links.resolve_ea(name)
    if ea == ida_idaapi.BADADDR:
        return None, "no such name/address in the IDB"
    func = ida_funcs.get_func(ea)
    if func is None:
        return None, "not inside a function"

    try:
        cfunc = ida_hexrays.decompile(func.start_ea)
    except ida_hexrays.DecompilationFailure as exc:
        return None, f"decompilation failed: {exc}"
    if cfunc is None:
        return None, "decompilation failed"

    raw = [ida_lines.tag_remove(sl.line) for sl in cfunc.get_pseudocode()]
    total = len(raw)
    lo = 1 if start is None else max(1, start)
    hi = total if end is None else min(total, end)
    if lo > hi or lo > total:
        return None, f"line range out of bounds (function has {total} lines)"
    return raw[lo - 1 : hi], None


def preview_text(name: str, line: int | None = None, context: int = 8) -> str:
    """Short pseudocode excerpt for hover tooltips.

    Without a line: the first `context` lines. With one: a window around it,
    the target marked with ►.
    """
    if line is not None:
        start = max(1, line - 2)
        end = start + context - 1
    else:
        start, end = 1, context

    lines, err = decompile_lines(name, start, end)
    if err is not None:
        return f"⚠ {name}: {err}"

    out = []
    for i, text in enumerate(lines):
        if line is not None:
            out.append(("► " if start + i == line else "  ") + text)
        else:
            out.append(text)
    if len(lines) >= end - start + 1:
        out.append("…")
    return "\n".join(out)


def _render_embed(match: re.Match) -> str:
    auto = bool(match.group(1))
    name = match.group(2)
    start_s, end_s, hl_s = match.group(3), match.group(4), match.group(5)

    start = int(start_s) if start_s else None
    if end_s is not None:
        end = int(end_s) if end_s else None          # "a:b", "a:", ":b"
    elif start is not None:
        end = start                                  # "[7]" → line 7 only
    else:
        end = None                                   # "[]" / "[@5]" → all
    highlight = int(hl_s) if hl_s else None

    try:
        lines, err = decompile_lines(name, start, end)
    except Exception:
        logger.exception("embed failed for %s", match.group(0))
        lines, err = None, "internal error (see Output window)"

    if err is not None:
        return f"\n```\n// {name}: {err}\n```\n"

    lo = start if start is not None else 1
    if highlight is not None and lo <= highlight < lo + len(lines):
        marked = []
        for i, text in enumerate(lines):
            prefix = "► " if lo + i == highlight else "  "
            marked.append(prefix + text)
        lines = marked

    if start is None and end is None:
        header = f"// {name}"
    else:
        header = f"// {name} [{start_s or 1}:{end_s if end_s else (end or '')}]"
    body = "\n".join(lines)

    # the presenter-follow caption is a normal @! link, so the standard
    # linkify/auto-jump machinery picks it up when the slide is shown
    target = highlight if highlight is not None else lo
    caption = f"\n@!{name}:{target}\n" if auto else ""
    return f"\n{caption}\n```c\n{header}\n{body}\n```\n"


def _expand_line(line: str) -> str:
    if "@" not in line or "[" not in line:
        return line
    spans = [m.span() for m in _INLINE_CODE_RE.finditer(line)]

    def _sub(match: re.Match) -> str:
        pos = match.start()
        if any(a <= pos < b for a, b in spans):
            return match.group(0)  # inside inline code — leave as-is
        return _render_embed(match)

    return EMBED_RE.sub(_sub, line)


def expand_embeds(text: str) -> str:
    """Expand all embed tokens in deck text, skipping fenced code blocks."""
    out = []
    fence: str | None = None
    for line in text.splitlines():
        m = _FENCE_RE.match(line)
        if m:
            marker = m.group(1)
            if fence is None:
                fence = marker
            elif fence == marker:
                fence = None
            out.append(line)
            continue
        out.append(line if fence is not None else _expand_line(line))
    return "\n".join(out) + ("\n" if text.endswith("\n") else "")
