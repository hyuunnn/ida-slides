"""Standalone macOS renderer tests — run OUTSIDE IDA:

    python3 tests/test_webkit_standalone.py

The WKWebView embedding and the marp watcher pipeline are the parts of the
plugin that can be exercised end-to-end from a plain Python process (needs
a GUI session, PySide6, pyobjc-framework-WebKit, and the marp CLI; each is
skipped cleanly when missing). Counterpart of test_webview2_standalone.py's
Part 2 — there is no Part-1 analog here on purpose: the macOS side has no
hand-written vtable/COM layer to smoke-test, and its crash-safety rests on
the structural rule documented in webkit_view.py (never implement a
delegate method that receives a block), which a smoke test cannot probe.

Covered end-to-end (no IDB needed — the deck carries no [a:b] embeds, and
unresolved @names still linkify, so the DOM probe works without IDA):

- native attach (attach_failed stays unset, _web comes up)
- marp -w pipeline: render-complete detection, USER_JS @token linkify
- reload() with hash capture
- save cycle: on_source_changed() re-renders in place
- rapid double-save: the FINAL content must win (regression for the
  stale-render latch fixed in 1b044df)
- JS→Python bridge: clicking an @link reaches do_jump via postMessage
- cleanup: watcher stopped, generated siblings removed

NOT covered (needs IDA): focus invariants, [a:b] embed decompilation,
dock behavior — those live in test_in_ida.py and manual checks.

Exit code 0 = all executed checks passed (skips are fine), 1 = failure.

Needs Python 3.10+ with PySide6 + pyobjc importable (the plugin modules use
`X | None` annotations); IDA's bundled python meets all three:

    /Applications/IDA*/Contents/MacOS/idapython3 … or the same venv/python
    you point IDA at. A python without PySide6 skips cleanly.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

_results: list[tuple[str, bool]] = []


def check(name: str, ok, detail: str = "") -> bool:
    ok = bool(ok)
    _results.append((name, ok))
    print(f"  [{'OK' if ok else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))
    return ok


_DECK_V0 = (
    "---\nmarp: true\n---\n\n# Analysis of @main\n\n"
    "see @sub_401000 and @main:12\n\nMARKER_V0\n\n---\n\n# Slide 2\n"
)


def _deck_with(marker: str) -> str:
    return _DECK_V0.replace("MARKER_V0", marker)


def qt_e2e() -> str | None:
    """Returns a skip reason, or None when the checks ran."""
    if sys.version_info < (3, 10):
        return f"needs Python 3.10+ (this is {sys.version.split()[0]})"
    try:
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QApplication, QMainWindow
    except ImportError:
        return "PySide6 not importable"

    import deck_view
    import webkit_view

    if not webkit_view.webkit_available():
        return "WKWebView unavailable (PyObjC not installed?)"
    if deck_view.find_marp() is None:
        return "marp CLI not installed"

    tmp = tempfile.mkdtemp(prefix="ida-slides-e2e-")
    deck = os.path.join(tmp, "talk.md")
    with open(deck, "w", encoding="utf-8") as f:
        f.write(_DECK_V0)

    app = QApplication.instance() or QApplication(sys.argv)
    win = QMainWindow()
    view = webkit_view.DeckWebKitView()
    win.setCentralWidget(view)
    win.resize(900, 700)
    win.show()

    jumps: list[tuple[str, object]] = []
    state = {"phase": "load", "err": None}
    deadline = time.monotonic() + 90
    # 3 @tokens in the deck; at least one bespoke svg/section per slide
    probe = (
        "document.querySelectorAll('a.ida-xref').length + '|' + "
        "document.querySelectorAll('svg,section').length + '|' + "
        "(document.body.innerText.indexOf('MARKER_V2') >= 0 ? 'v2' : "
        " document.body.innerText.indexOf('MARKER_V1') >= 0 ? 'v1' : 'v0')"
    )

    check("load_returned_true", view.load(deck))

    def fail(reason: str) -> None:
        state["err"] = f"{reason} (status: {view._status.text()!r})"
        app.exit(1)

    def advance(xrefs: int, marker: str) -> None:
        phase = state["phase"]
        if phase == "load":
            check("attach_ok", not getattr(view, "attach_failed", False))
            check("initial_render_xrefs", xrefs >= 3, f"xrefs={xrefs}")
            check("engine_label", view.engine_label == "Marp", view.engine_label)
            state["phase"] = "reload"
            view.reload()  # exercises hash capture + re-render/reload path
        elif phase == "reload":
            check("after_reload_xrefs", xrefs >= 3, f"xrefs={xrefs}")
            state["phase"] = "save"
            with open(deck, "w", encoding="utf-8") as f:
                f.write(_deck_with("MARKER_V1"))
            view.on_source_changed()  # what the file watcher would do
        elif phase == "save" and marker == "v1":
            check("save_rerendered", True)
            state["phase"] = "doublesave"
            # two saves in quick succession: the SECOND must win even if
            # its sibling rewrite lands while marp is mid-render
            with open(deck, "w", encoding="utf-8") as f:
                f.write(_deck_with("MARKER_V1 then"))
            view.on_source_changed()
            with open(deck, "w", encoding="utf-8") as f:
                f.write(_deck_with("MARKER_V2"))
            view.on_source_changed()
        elif phase == "doublesave" and marker == "v2":
            check("double_save_latest_wins", True)
            state["phase"] = "click"
            view.do_jump = lambda name, line=None: jumps.append((name, line))
            view._native_eval_js(
                "document.querySelector('a.ida-xref').click();"
            )
        elif phase == "click":
            if jumps:
                check("bridge_click_reached_do_jump", True, repr(jumps[0]))
                app.exit(0)

    def poll():
        if time.monotonic() > deadline:
            fail(f"timeout in phase {state['phase']!r}")
            return
        if view._web is None:
            QTimer.singleShot(500, poll)
            return
        if state["phase"] == "click" and jumps:
            advance(0, "")
            return

        def got(result):
            try:
                if result and result.count("|") == 2:
                    xrefs_s, sections_s, marker = result.split("|")
                    if int(xrefs_s) >= 1 and int(sections_s) >= 1:
                        advance(int(xrefs_s), marker)
            except Exception as exc:
                print("  probe parse error:", exc)
            QTimer.singleShot(400, poll)

        try:
            view._native_eval_js_result(probe, got)
        except Exception as exc:  # keep polling; the deadline reports
            print("  probe error:", exc)
            QTimer.singleShot(400, poll)

    QTimer.singleShot(500, poll)
    app.exec()

    if state["err"]:
        check("e2e", False, state["err"])
    elif state["phase"] != "click" or not jumps:
        check("e2e", False, f"stalled in phase {state['phase']!r}")

    sib_md = view._sibling(deck, "md")
    sib_html = view._sibling(deck, "html")
    view.cleanup()
    check("marp_watcher_stopped", view._proc is None)
    check(
        "generated_siblings_removed",
        not os.path.exists(sib_md) and not os.path.exists(sib_html),
    )
    win.close()
    shutil.rmtree(tmp, ignore_errors=True)
    return None


def main() -> int:
    if sys.platform != "darwin":
        print("SKIP: macOS only (the Windows renderer has its own harness)")
        return 0

    print("DeckWebKitView + marp E2E (Qt)")
    skip = qt_e2e()
    if skip:
        print(f"  SKIP: {skip}")

    failed = [n for n, ok in _results if not ok]
    print(f"\n{len(_results) - len(failed)} passed, {len(failed)} failed"
          + (f": {failed}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
