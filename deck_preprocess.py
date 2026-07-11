"""Markdown preprocessing: expand `@name[a:b]` tokens into fenced code
blocks holding the Hex-Rays pseudocode of that function.

Runs before the deck reaches the rendering engine (marp CLI or slidev), so
embeds work identically in both. Supported forms:

    @sub_401000[1:5]     lines 1-5 of the pseudocode
    @sub_401000[:5]      lines 1-5
    @sub_401000[3:]      line 3 to the end
    @sub_401000[7]       line 7 only
    @sub_401000[]        the whole function
    @sub_401000[1:8@5]   lines 1-8 with line 5 marked (►)

Tokens inside fenced code blocks or inline backtick spans are left alone so
decks can document the syntax itself.
"""

import logging
import re

import ida_links
import marp_markdown

logger = logging.getLogger(__name__)

_INLINE_CODE_RE = re.compile(r"`[^`]*`")

EMBED_RE = re.compile(
    rf"(?<![A-Za-z0-9_@])@({ida_links._NAME_PATTERN})"
    rf"\[(\d*)(?::(\d*))?(?:@(\d+))?\]"
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

    sv = cfunc.get_pseudocode()
    total = sv.size()
    lo = 1 if start is None else max(1, start)
    hi = total if end is None else min(total, end)
    if lo > hi or lo > total:
        return None, f"line range out of bounds (function has {total} lines)"
    # strip color tags only from the requested lines — a one-line embed
    # (or an 8-line hover preview) of a huge function must not pay for
    # converting every line
    return [ida_lines.tag_remove(sv[i].line) for i in range(lo - 1, hi)], None


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
    name = match.group(1)
    start_s, end_s, hl_s = match.group(2), match.group(3), match.group(4)

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
    return f"\n```c\n{header}\n{body}\n```\n"


def unresolved_refs(text: str) -> list[tuple[int, str]]:
    """(slide_no, "@token") pairs whose names the open IDB can't resolve.

    Slide numbers are 1-based, split by `marp_markdown.split_slides`, so
    they match the deck the presenter sees. Trailing dots are trimmed the
    same way the injected linkifier does, so sentence punctuation after a
    token isn't reported as part of the name.
    """
    import ida_idaapi
    import ida_segment

    # one IDB lookup per distinct name for the whole pass: the same token
    # tends to appear on many slides, and the trim loop below re-asks too
    ok_cache: dict[str, bool] = {}

    def _ok(name: str) -> bool:
        cached = ok_cache.get(name)
        if cached is not None:
            return cached
        ea = ida_links.resolve_ea(name)
        if ea == ida_idaapi.BADADDR:
            ok = False
        elif name.lower().startswith("0x"):
            # raw hex parses unconditionally; only mapped addresses jump
            ok = ida_segment.getseg(ea) is not None
        else:
            ok = True
        ok_cache[name] = ok
        return ok

    out: list[tuple[int, str]] = []
    for idx, slide in enumerate(marp_markdown.parse_deck(text), start=1):
        names = {m.group(1) for m in EMBED_RE.finditer(slide)}
        for m in ida_links.TOKEN_RE.finditer(slide):
            name = m.group(1)
            while name.endswith(".") and not _ok(name):
                name = name[:-1]
            if name:
                names.add(name)
        out.extend((idx, f"@{n}") for n in sorted(names) if not _ok(n))
    return out


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
    out = [
        line if in_code else _expand_line(line)
        for line, in_code in marp_markdown.iter_fenced(text.splitlines())
    ]
    return "\n".join(out) + ("\n" if text.endswith("\n") else "")
