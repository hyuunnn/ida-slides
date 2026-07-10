"""Turn `@name` / `@0xADDR` tokens into clickable links that jump IDA views.

Shared by both renderers:
- the QTextBrowser renderer calls `linkify_html()` on server-side rendered HTML
- the QWebEngineView renderer injects `LINKIFY_JS` into the loaded Marp deck

Both emit `ida:///<percent-encoded-name>` URLs. The name lives in the URL
*path* (triple slash) because QUrl lowercases the host part, which would
corrupt case-sensitive IDA names like `@sub_DEADBEEF`.
"""

import logging
import re
import urllib.parse

logger = logging.getLogger(__name__)

IDA_URL_SCHEME = "ida"

# @sub_401000, @main, @_Z3foov, @.init_proc, @0x401000
# an optional :N suffix targets pseudocode line N (@sub_401000:22);
# the lookbehind keeps the @ in emails (user@host) from starting a token
_NAME_PATTERN = r"0x[0-9A-Fa-f]+|[A-Za-z_?$.][\w?$@.]*"
TOKEN_RE = re.compile(rf"(?<![A-Za-z0-9_@])@({_NAME_PATTERN})(?::(\d+))?")

_TAG_SPLIT_RE = re.compile(r"(<[^>]*>)")


def make_href(name: str, line: int | None = None) -> str:
    href = f"{IDA_URL_SCHEME}:///{urllib.parse.quote(name, safe='')}"
    return f"{href}:{line}" if line else href


def name_from_url(url) -> tuple[str, int | None] | None:
    """Extract (name, line) from a QUrl if it is an ida:/// link, else None."""
    if url.scheme() != IDA_URL_SCHEME:
        return None
    spec = urllib.parse.unquote(url.path().lstrip("/"))
    if not spec:
        return None
    name, _, line_s = spec.rpartition(":")
    if name and line_s.isdigit():
        return name, int(line_s)
    return spec, None


def resolve_ea(name: str) -> int:
    import ida_idaapi
    import ida_name

    if name.lower().startswith("0x"):
        try:
            return int(name, 16)
        except ValueError:
            return ida_idaapi.BADADDR
    return ida_name.get_name_ea(ida_idaapi.BADADDR, name)


def is_resolvable(name: str) -> bool:
    import ida_idaapi

    return resolve_ea(name) != ida_idaapi.BADADDR


def jump_to(name: str, line: int | None = None) -> bool:
    import ida_idaapi
    import ida_kernwin

    ea = resolve_ea(name)
    if ea == ida_idaapi.BADADDR:
        ida_kernwin.msg(f"ida-slides: no such name/address: {name}\n")
        return False
    if line is None:
        return _jump_no_focus(ea)
    return _jump_to_pseudocode_line(ea, line, name)


def _jump_no_focus(ea: int) -> bool:
    """jumpto without UIJMP_ACTIVATE, so keyboard focus (and arrow-key
    slide control) stays on the presenter instead of the IDA view."""
    import ida_kernwin

    ok = ida_kernwin.jumpto(ea, -1, 0)
    w = ida_kernwin.find_widget("IDA View-A")
    if w is not None:
        # a non-activating jump repositions a buried tab but does not
        # raise it; raise explicitly while leaving focus alone
        ida_kernwin.activate_widget(w, False)
        return ok
    # no default disasm view to surface — let an activating jump open
    # one, then hand focus straight back to whoever had it
    prev = ida_kernwin.get_current_widget()
    ok = ida_kernwin.jumpto(ea)
    if prev is not None:
        ida_kernwin.activate_widget(prev, True)
    return ok


def _jump_to_pseudocode_line(ea: int, line: int, name: str) -> bool:
    """Open the decompiler view for `ea` positioned at 1-indexed `line`."""
    import ida_kernwin

    try:
        import ida_hexrays

        # open_pseudocode focuses the pseudocode view even when reusing
        # an existing one; hand focus straight back to the deck
        prev = ida_kernwin.get_current_widget()
        vu = ida_hexrays.open_pseudocode(ea, ida_hexrays.OPF_REUSE)
        if vu is None:
            raise RuntimeError("open_pseudocode failed")
        if prev is not None:
            ida_kernwin.activate_widget(prev, True)
        nlines = vu.cfunc.get_pseudocode().size()
        lnnum = min(max(line, 1), nlines) - 1
        ct = vu.ct

        from PySide6.QtCore import QTimer

        def _position(attempt: int = 0) -> None:
            # Opening a *different* function queues Hex-Rays' own entry-point
            # jump, which can land after ours and clobber it. We re-apply and
            # verify a few times until the caret sticks (or give up quietly).
            try:
                # raise the pseudocode tab but keep keyboard focus on the
                # deck (take_focus=False); the retry loop below would
                # otherwise re-steal focus on every attempt
                ida_kernwin.activate_widget(ct, False)
                # simpleline_place_t's constructor is abstract in IDA 9.3's
                # bindings; clone the viewer's place and cast it. NB: the cast
                # returns a fresh proxy each call, so the object we mutate must
                # be the one we hand to jumpto — mutating a throwaway is a no-op.
                clone = ida_kernwin.get_custom_viewer_place(ct, False)[0].clone()
                sp = ida_kernwin.place_t_as_simpleline_place_t(clone)
                sp.n = lnnum
                ida_kernwin.jumpto(ct, sp, 0, 0)

                cur, _x, _y = ida_kernwin.get_custom_viewer_place(ct, False)
                landed = ida_kernwin.place_t_as_simpleline_place_t(cur).n
                if landed != lnnum and attempt < 6:
                    QTimer.singleShot(60, lambda: _position(attempt + 1))
            except Exception:
                if attempt < 6:
                    QTimer.singleShot(60, lambda: _position(attempt + 1))
                else:
                    logger.exception("pseudocode line positioning failed")

        QTimer.singleShot(60, _position)
        return True
    except Exception:
        logger.exception("pseudocode line jump failed for %s:%d", name, line)
        ida_kernwin.msg(
            f"ida-slides: cannot open pseudocode for {name}; "
            "jumping to the function instead\n"
        )
        return _jump_no_focus(ea)


def linkify_html(
    html: str,
    is_resolved=is_resolvable,
    link_color: str = "#4ea1ff",
    link_bg: str = "#2a3b52",
    unresolved_color: str = "#9aa0a6",
) -> str:
    """Replace @tokens in the text portions of `html` with styled anchors.

    Tokens inside tag markup are untouched; tokens inside <pre>/<code> text
    are linkified on purpose so decks can show clickable snippets.
    Inline styles are used because QTextBrowser's CSS support for class
    selectors is unreliable.
    """

    def _sub(m: re.Match) -> str:
        name = m.group(1)
        line = int(m.group(2)) if m.group(2) else None
        # trailing dots are almost always sentence punctuation, not part of
        # the name ("... @main. Next ...")
        trail = ""
        while name.endswith(".") and not is_resolved(name):
            name = name[:-1]
            trail += "."
        if not name:
            return m.group(0)
        token = "@" + name + (f":{line}" if line else "")
        if is_resolved(name):
            return (
                f'<a href="{make_href(name, line)}" style="'
                f"color:{link_color}; background-color:{link_bg}; "
                f'text-decoration:none; font-family:monospace;">{token}</a>{trail}'
            )
        return (
            f'<span style="color:{unresolved_color}; font-family:monospace;">'
            f"{token}</span>{trail}"
        )

    parts = _TAG_SPLIT_RE.split(html)
    for i, part in enumerate(parts):
        if part.startswith("<"):
            continue
        parts[i] = TOKEN_RE.sub(_sub, part)
    return "".join(parts)


# Injected into Marp HTML decks rendered by QWebEngineView. Wraps @tokens in
# anchors; clicks are intercepted by acceptNavigationRequest on the page.
LINKIFY_JS = r"""
(function () {
    if (window.__idaPptLinkified) return;
    window.__idaPptLinkified = true;

    var RE = /@(0x[0-9A-Fa-f]+|[A-Za-z_?$.][\w?$@.]*)(?::(\d+))?/g;

    var style = document.createElement('style');
    style.textContent =
        'a.ida-xref{color:#4ea1ff;background:rgba(78,161,255,.15);' +
        'border-radius:3px;padding:0 .15em;text-decoration:none;' +
        'font-family:monospace;}' +
        'a.ida-xref:hover{background:rgba(78,161,255,.35);}';
    document.head.appendChild(style);

    var walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    var targets = [];
    while (walker.nextNode()) {
        var n = walker.currentNode;
        if (n.parentElement && n.parentElement.closest('a,script,style')) continue;
        RE.lastIndex = 0;
        if (RE.test(n.nodeValue)) targets.push(n);
    }
    targets.forEach(function (n) {
        var span = document.createElement('span');
        var escaped = n.nodeValue.replace(/[&<>]/g, function (c) {
            return { '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c];
        });
        RE.lastIndex = 0;
        span.innerHTML = escaped.replace(RE, function (m, name, line, offset, str) {
            var prev = offset > 0 ? str.charAt(offset - 1) : '';
            if (/[A-Za-z0-9_@]/.test(prev)) return m;   // user@host etc.
            var trail = '';
            while (name.length && name.slice(-1) === '.') {
                name = name.slice(0, -1);
                trail += '.';
            }
            if (!name.length) return m;
            var label = '@' + name + (line ? ':' + line : '');
            return '<a class="ida-xref" href="ida:///' +
                encodeURIComponent(name) + (line ? ':' + line : '') +
                '">' + label + '</a>' + trail;
        });
        n.parentNode.replaceChild(span, n);
    });
})();
"""
