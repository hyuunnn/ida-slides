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
_NAME_PATTERN = r"0x[0-9A-Fa-f]+|[A-Za-z_?$.][\w?$@.]*"
TOKEN_RE = re.compile(rf"@({_NAME_PATTERN})")

_TAG_SPLIT_RE = re.compile(r"(<[^>]*>)")


def make_href(name: str) -> str:
    return f"{IDA_URL_SCHEME}:///{urllib.parse.quote(name, safe='')}"


def name_from_url(url) -> str | None:
    """Extract the IDA name from a QUrl if it is an ida:/// link, else None."""
    if url.scheme() != IDA_URL_SCHEME:
        return None
    name = urllib.parse.unquote(url.path().lstrip("/"))
    return name or None


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


def jump_to(name: str) -> bool:
    import ida_idaapi
    import ida_kernwin

    ea = resolve_ea(name)
    if ea == ida_idaapi.BADADDR:
        ida_kernwin.msg(f"ida-slides: no such name/address: {name}\n")
        return False
    return ida_kernwin.jumpto(ea)


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
        # trailing dots are almost always sentence punctuation, not part of
        # the name ("... @main. Next ...")
        trail = ""
        while name.endswith(".") and not is_resolved(name):
            name = name[:-1]
            trail += "."
        if not name:
            return m.group(0)
        token = "@" + name
        if is_resolved(name):
            return (
                f'<a href="{make_href(name)}" style="'
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

    var RE = /@(0x[0-9A-Fa-f]+|[A-Za-z_?$.][\w?$@.]*)/g;

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
        span.innerHTML = escaped.replace(RE, function (m, name) {
            var trail = '';
            while (name.length && name.slice(-1) === '.') {
                name = name.slice(0, -1);
                trail += '.';
            }
            if (!name.length) return m;
            return '<a class="ida-xref" href="ida:///' +
                encodeURIComponent(name) + '">@' + name + '</a>' + trail;
        });
        n.parentNode.replaceChild(span, n);
    });
})();
"""
