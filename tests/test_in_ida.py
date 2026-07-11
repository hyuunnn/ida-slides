"""In-IDA regression tests for ida-slides.

Run inside IDA (the plugin is tied to ida_kernwin / Qt / WebKit, so it can't
run under a bare `pytest`). Open any IDB, then either:

    - IDA Python console:  exec(open("<repo>/tests/test_in_ida.py").read())
    - or import and call:  import test_in_ida; test_in_ida.run()

Pure-logic checks (token grammar, slide splitting, front-matter parsing,
embed/lint text handling) always run. Checks that need a database (name
resolution, decompilation, live lint) run only when an IDB with at least one
decompilable function is loaded, and are skipped otherwise. No fixed binary
is assumed — a suitable function is picked from whatever IDB is open.
"""

import os
import sys
import tempfile
import traceback

# make the plugin modules importable when run as a loose script
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

import deck_preprocess
import ida_links
import marp_markdown
import webkit_view


class _Runner:
    def __init__(self):
        self.passed = 0
        self.failed = 0
        self.skipped = 0

    def check(self, name, fn):
        try:
            fn()
        except _Skip as exc:
            self.skipped += 1
            print(f"  SKIP {name}: {exc}")
        except Exception:
            self.failed += 1
            print(f"  FAIL {name}")
            print("    " + traceback.format_exc().replace("\n", "\n    ").rstrip())
        else:
            self.passed += 1
            print(f"  ok   {name}")


class _Skip(Exception):
    pass


def eq(got, want):
    if got != want:
        raise AssertionError(f"got {got!r}, want {want!r}")


def truthy(got, msg=""):
    if not got:
        raise AssertionError(f"expected truthy {msg}: {got!r}")


# ---------------------------------------------------------------------------
# Pure-logic tests (no IDB required)
# ---------------------------------------------------------------------------
def test_token_regex():
    def names(text):
        return [m.group(1) for m in ida_links.TOKEN_RE.finditer(text)]

    eq(names("see @main and @sub_401000 and @0x401000"),
       ["main", "sub_401000", "0x401000"])
    eq([(m.group(1), m.group(2)) for m in ida_links.TOKEN_RE.finditer("@main:12")],
       [("main", "12")])
    # an email's @ must not start a token
    eq(names("mail me at user@example.com please"), [])
    # @ right after a word char is not a token either
    eq(names("foo@bar"), [])


def test_js_token_re_substituted():
    truthy("__IDA_TOKEN_RE__" not in webkit_view.USER_JS, "USER_JS")
    truthy(ida_links.JS_TOKEN_RE.startswith("/@("), ida_links.JS_TOKEN_RE)


def test_split_slides_setext():
    # '---' directly under a text line is a setext H2, not a page break
    eq(len(marp_markdown.split_slides("Overview\n---\nbody text")), 1)
    # a blank line before '---' makes it a real separator
    eq(len(marp_markdown.split_slides("Overview\n\n---\n\nbody text")), 2)


def test_split_slides_fence_length():
    # a 4-backtick fence is not closed by an inner ``` line
    deck = "````markdown\n```c\nx=1;\n```\n---\nstill inside\n````\n\n---\n\nB"
    slides = marp_markdown.split_slides(deck)
    eq(len(slides), 2)
    truthy("still inside" in slides[0], "4-backtick fence held")
    # a tilde fence is not closed by a backtick fence
    eq(len(marp_markdown.split_slides("~~~\n```\n---\n~~~\n\n---\n\nB")), 2)


# >100 front-matter lines: the old engine-detection scanner capped its scan
# at 100 and silently dropped everything below (shared by the two tests so
# the fixtures can't drift apart)
_LONG_FM = "\n".join(f"k{i}: 1" for i in range(150))


def test_strip_front_matter():
    eq(marp_markdown.strip_front_matter("---\nmarp: true\n---\n# t\n").strip(), "# t")
    eq(marp_markdown.strip_front_matter("no front matter\n"), "no front matter\n")
    # an indented '  ---' is not a closing delimiter (matches marp/YAML)
    eq(marp_markdown.strip_front_matter("---\na: 1\n  ---\nb: 2\n---\nbody\n"),
       "body\n")


def test_front_matter_lines():
    eq(marp_markdown.front_matter_lines("---\na: 1\nb: 2\n---\nbody"),
       ["a: 1", "b: 2"])
    eq(marp_markdown.front_matter_lines("no front matter"), [])
    # same boundary rule as strip_front_matter: indented close ignored
    eq(marp_markdown.front_matter_lines("---\na: 1\n  ---\nb: 2\n---\nbody\n"),
       ["a: 1", "  ---", "b: 2"])
    # no length cap
    eq(len(marp_markdown.front_matter_lines(f"---\n{_LONG_FM}\n---\nbody")), 150)


def test_detect_engine_long_front_matter():
    # the explicit override must win even past 100 front-matter lines
    eq(_detect(f"ida-slides-engine: slidev\n{_LONG_FM}"), "slidev")


def test_bespoke_restore_js():
    js = marp_markdown.bespoke_restore_js("#/3")
    truthy('"#/3"' in js, js)          # json-encoded, not repr
    truthy("#/0" in js, js)            # flips through slide 0 first


def test_yaml_scalar():
    eq(webkit_view._yaml_scalar("false # opt out"), "false")
    eq(webkit_view._yaml_scalar('"false"'), "false")
    eq(webkit_view._yaml_scalar("'false'"), "false")
    eq(webkit_view._yaml_scalar("'a#b'"), "a#b")   # '#' inside quotes kept
    eq(webkit_view._yaml_scalar("true"), "true")


def test_node_version_key():
    # numeric ordering: a plain string sort would rank v9.* above v22.*
    old = "/Users/u/.nvm/versions/node/v9.11.0/bin/marp"
    new = "/Users/u/.nvm/versions/node/v22.1.0/bin/marp"
    truthy(
        webkit_view._node_version_key(new) > webkit_view._node_version_key(old),
        "v22 outranks v9",
    )
    eq(webkit_view._node_version_key(new), (22, 1, 0))
    eq(webkit_view._node_version_key("/opt/homebrew/bin/marp"), ())


def _detect(front_matter):
    fd, path = tempfile.mkstemp(suffix=".md")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(f"---\n{front_matter}\n---\n\n# t\n")
        return webkit_view.detect_engine(path)
    finally:
        os.unlink(path)


def test_detect_engine_marp():
    eq(_detect("marp: true"), "marp")
    eq(_detect("title: hello"), "marp")                 # default
    eq(_detect("ida-slides-engine: marp\ntransition: x"), "marp")


def test_detect_engine_slidev():
    # marp:false should defer to slidev — only assertable when slidev exists
    if not webkit_view.find_slidev():
        raise _Skip("slidev CLI not installed")
    eq(_detect("marp: false # opt out\ntransition: slide"), "slidev")
    eq(_detect("marp: false\ntransition: slide"), "slidev")


def test_embed_regex_and_fences():
    m = deck_preprocess.EMBED_RE.search("@sub_1[1:8@5]")
    truthy(m, "EMBED_RE match")
    eq((m.group(1), m.group(2), m.group(3), m.group(4)), ("sub_1", "1", "8", "5"))
    # tokens inside a fenced block / inline code are left untouched
    fenced = "````markdown\n```c\n@foo[1:2]\n```\n````\ndone"
    eq(deck_preprocess.expand_embeds(fenced).rstrip("\n"), fenced)
    inline = "use `@foo[1:2]` to embed"
    eq(deck_preprocess.expand_embeds(inline).rstrip("\n"), inline)


def test_file_watcher_stale_path():
    from file_watcher import DebouncedFileWatcher

    w = DebouncedFileWatcher()
    fd, path = tempfile.mkstemp(suffix=".md")
    os.close(fd)
    try:
        w.watch(path)
        # a fileChanged queued for a previously watched file must be
        # dropped entirely — restarting the debounce here used to emit a
        # phantom changed() for the NEW path
        w._on_file_changed(path + ".other")
        truthy(not w._timer.isActive(), "stale signal must not arm debounce")
        w._on_file_changed(path)
        truthy(w._timer.isActive(), "matching signal arms debounce")
    finally:
        w.unwatch()
        w.deleteLater()
        os.unlink(path)


def test_copy_ref_name_validation():
    import copy_ref
    truthy(copy_ref._NAME_OK.match("sub_401000"), "sub_")
    truthy(copy_ref._NAME_OK.match("_ZN3Foo3barEv"), "mangled")
    truthy(not copy_ref._NAME_OK.match("-[MyController viewDidLoad:]"), "objc")
    truthy(not copy_ref._NAME_OK.match("operator new(ulong)"), "demangled")


# ---------------------------------------------------------------------------
# IDB-dependent tests (need a loaded database with a decompilable function)
# ---------------------------------------------------------------------------
def _pick_function():
    """A (name, ea) for a decompilable function in the open IDB, or skip."""
    try:
        import idautils
        import ida_name
        import ida_hexrays
    except Exception as exc:
        raise _Skip(f"no IDA database ({exc})")
    if not ida_hexrays.init_hexrays_plugin():
        raise _Skip("Hex-Rays not available")
    for ea in idautils.Functions():
        name = ida_name.get_name(ea)
        if not name:
            continue
        try:
            if ida_hexrays.decompile(ea) is not None:
                return name, ea
        except Exception:
            continue
    raise _Skip("no decompilable function in this IDB")


def test_resolve_ea():
    import ida_idaapi
    name, ea = _pick_function()
    eq(ida_links.resolve_ea(name), ea)
    truthy(ida_links.is_resolvable(name), name)
    truthy(not ida_links.is_resolvable("no_such_name_zzz_123"), "bogus name")
    eq(ida_links.resolve_ea("no_such_name_zzz_123"), ida_idaapi.BADADDR)


def test_decompile_lines():
    name, _ = _pick_function()
    lines, err = deck_preprocess.decompile_lines(name, 1, 3)
    eq(err, None)
    truthy(1 <= len(lines) <= 3, f"sliced {len(lines)} lines")
    # out-of-range → error, not a crash
    _, err2 = deck_preprocess.decompile_lines(name, 100000, 100001)
    truthy(err2, "out-of-range error")
    # unknown name → error
    _, err3 = deck_preprocess.decompile_lines("no_such_name_zzz", 1, 2)
    truthy(err3, "unknown-name error")


def test_preview_text():
    name, _ = _pick_function()
    truthy(deck_preprocess.preview_text(name).strip(), "preview non-empty")


def test_expand_embeds_live():
    name, _ = _pick_function()
    out = deck_preprocess.expand_embeds(f"@{name}[1:2]")
    truthy("```c" in out, "embed produced a code block")
    truthy(name in out, "embed header names the function")


def test_unresolved_refs_live():
    name, _ = _pick_function()
    # a resolvable ref → nothing flagged; a bogus one → flagged with its slide
    deck = f"# s1\n\n@{name}\n\n---\n\n# s2\n\n@no_such_ref_zzz\n"
    issues = deck_preprocess.unresolved_refs(deck)
    eq(issues, [(2, "@no_such_ref_zzz")])


ALL = [
    ("token_regex", test_token_regex),
    ("js_token_re_substituted", test_js_token_re_substituted),
    ("split_slides_setext", test_split_slides_setext),
    ("split_slides_fence_length", test_split_slides_fence_length),
    ("strip_front_matter", test_strip_front_matter),
    ("front_matter_lines", test_front_matter_lines),
    ("detect_engine_long_front_matter", test_detect_engine_long_front_matter),
    ("bespoke_restore_js", test_bespoke_restore_js),
    ("yaml_scalar", test_yaml_scalar),
    ("node_version_key", test_node_version_key),
    ("detect_engine_marp", test_detect_engine_marp),
    ("detect_engine_slidev", test_detect_engine_slidev),
    ("embed_regex_and_fences", test_embed_regex_and_fences),
    ("file_watcher_stale_path", test_file_watcher_stale_path),
    ("copy_ref_name_validation", test_copy_ref_name_validation),
    ("resolve_ea", test_resolve_ea),
    ("decompile_lines", test_decompile_lines),
    ("preview_text", test_preview_text),
    ("expand_embeds_live", test_expand_embeds_live),
    ("unresolved_refs_live", test_unresolved_refs_live),
]


def run():
    r = _Runner()
    print("ida-slides tests")
    for name, fn in ALL:
        r.check(name, fn)
    print(f"\n{r.passed} passed, {r.failed} failed, {r.skipped} skipped")
    return r.failed == 0


if __name__ == "__main__":
    run()
