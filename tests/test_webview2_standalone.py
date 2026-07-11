"""Standalone Windows renderer tests — run OUTSIDE IDA:

    python tests\\test_webview2_standalone.py

Unlike test_in_ida.py, nothing here needs an IDB: the WebView2 embedding is
the one part of the plugin that can be exercised end-to-end from a plain
Python process, and this is the harness to run BEFORE touching any
vtable/COM code (a wrong slot index shows up here as a clean failure
instead of a hard crash inside IDA).

Part 1 — COM layer (always, Windows only): drives webview2_com against the
real WebView2 runtime in a bare Win32 window, no Qt: environment →
controller → user-script injection → postMessage bridge → ExecuteScript
round-trip → new-window suppression → teardown.

Part 2 — renderer E2E (needs PySide6 + the marp CLI; skipped otherwise):
QApplication + DeckWebView2View + the real marp -w pipeline: tool
discovery, render-complete detection, navigation, USER_JS linkification of
@tokens in the DOM, and reload() with hash capture.

Exit code 0 = all executed checks passed (skips are fine), 1 = failure.
"""

import ctypes
import os
import shutil
import sys
import tempfile
import time
from ctypes import wintypes

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

_results: list[tuple[str, bool]] = []


def check(name: str, ok, detail: str = "") -> bool:
    ok = bool(ok)
    _results.append((name, ok))
    print(f"  [{'OK' if ok else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))
    return ok


# PRIVATE user-data folders, never the plugin's: a WebView2 user-data
# folder is owned by one host process's browser instance, and attaching
# from a second process fails with ERROR_INVALID_STATE (0x8007139f) — so
# sharing the plugin's folder would make this test fail whenever an IDA
# with ida-slides is running, which is exactly when you want to run it
def _private_udf() -> str:
    return tempfile.mkdtemp(prefix="ida-slides-test-udf-")


# ---------------------------------------------------------------------------
# Part 1: ctypes COM layer in a bare Win32 window
# ---------------------------------------------------------------------------
def com_smoke() -> None:
    import webview2_com as wv2

    user32 = ctypes.WinDLL("user32")
    loader = os.path.join(_REPO, "win", "WebView2Loader.dll")

    ver = wv2.runtime_version(loader)
    if not check("runtime_version", ver, ver or "runtime/loader missing"):
        return

    hwnd = user32.CreateWindowExW(
        0, "STATIC", "wv2-smoke", 0x10CF0000,  # WS_OVERLAPPEDWINDOW|WS_VISIBLE
        100, 100, 800, 600, None, None, None, None,
    )
    if not check("host_window", hwnd):
        return

    tmp = tempfile.mkdtemp(prefix="ida-slides-wv2-")
    test_html = os.path.join(tmp, "smoke.html")
    with open(test_html, "w", encoding="utf-8") as f:
        f.write(
            "<!doctype html><html><body><h1>wv2 smoke</h1>\n"
            '<a id="ext" href="https://example.com/" target="_blank">ext</a>\n'
            "<script>\n"
            "  window.chrome.webview.postMessage("
            "{type: 'hello', injected: window.__injected});\n"
            "  setTimeout(function(){"
            "document.getElementById('ext').click();}, 300);\n"
            "</script></body></html>"
        )

    state = {"env": None, "ctrl": None, "web": None, "navs": 0,
             "msgs": [], "exec": [], "newwin": []}

    def on_env(env, hr):
        check("environment", env is not None, f"hr=0x{hr & 0xFFFFFFFF:08x}")
        if env is None:
            return
        state["env"] = env
        env.create_controller(hwnd, on_ctrl)

    def on_ctrl(ctrl, hr):
        check("controller", ctrl is not None, f"hr=0x{hr & 0xFFFFFFFF:08x}")
        if ctrl is None:
            return
        state["ctrl"] = ctrl
        ctrl.put_bounds(0, 0, 800, 560)
        ctrl.put_is_visible(True)
        web = ctrl.get_core_webview2()
        if not check("get_core_webview2", web is not None):
            return
        state["web"] = web
        web.add_script_to_execute_on_document_created("window.__injected = 42;")
        web.add_web_message_received(lambda j: state["msgs"].append(j))
        web.add_navigation_completed(on_nav)
        web.add_new_window_requested(lambda u: state["newwin"].append(u))
        web.navigate("file:///" + test_html.replace("\\", "/"))

    def on_nav():
        state["navs"] += 1
        if state["navs"] == 1:
            state["web"].execute_script(
                "1+2", lambda r: state["exec"].append(r)
            )

    udf = _private_udf()
    wv2.create_environment(loader, udf, on_env)

    # pump until everything arrived or the deadline passes
    msg = wintypes.MSG()
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        if state["msgs"] and state["exec"] and state["newwin"]:
            break
        time.sleep(0.01)

    check("navigation_completed", state["navs"] >= 1, f"navs={state['navs']}")
    check(
        "user_script_and_bridge",
        any("42" in m and "hello" in m for m in state["msgs"]),
        str(state["msgs"][:3]),
    )
    check("execute_script", state["exec"] == ["3"], str(state["exec"]))
    check(
        "new_window_suppressed",
        any("example.com" in u for u in state["newwin"]),
        str(state["newwin"]),
    )

    # the teardown sequence the plugin itself uses
    if state["ctrl"]:
        state["ctrl"].close()
        state["ctrl"].release()
    if state["web"]:
        state["web"].release()
    if state["env"]:
        state["env"].release()
    user32.DestroyWindow(hwnd)
    shutil.rmtree(tmp, ignore_errors=True)
    # the browser may still hold cache files for a moment — best effort
    shutil.rmtree(udf, ignore_errors=True)


# ---------------------------------------------------------------------------
# Part 2: DeckWebView2View + the real marp pipeline under Qt
# ---------------------------------------------------------------------------
def qt_e2e() -> str | None:
    """Returns a skip reason, or None when the checks ran."""
    try:
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QApplication, QMainWindow
    except ImportError:
        return "PySide6 not importable"

    # private user-data folder for the renderer too (see _private_udf)
    udf = _private_udf()
    os.environ["IDA_SLIDES_WEBVIEW2_UDF"] = udf

    import deck_view
    import webview2_view

    if webview2_view.availability_error() is not None:
        return "WebView2 unavailable"
    if deck_view.find_marp() is None:
        return "marp CLI not installed"

    tmp = tempfile.mkdtemp(prefix="ida-slides-e2e-")
    deck = os.path.join(tmp, "talk.md")
    with open(deck, "w", encoding="utf-8") as f:
        f.write(
            "---\nmarp: true\n---\n\n# Analysis of @main\n\n"
            "see @sub_401000 and @main:12\n\n---\n\n# Slide 2\n"
        )

    app = QApplication.instance() or QApplication(sys.argv)
    win = QMainWindow()
    view = webview2_view.DeckWebView2View()
    win.setCentralWidget(view)
    win.resize(900, 700)
    win.show()

    state = {"phase": "load", "err": None}
    deadline = time.monotonic() + 90
    # 3 @tokens in the deck; at least one bespoke svg/section per slide
    probe = (
        "document.querySelectorAll('a.ida-xref').length + '|' + "
        "document.querySelectorAll('svg,section').length"
    )

    check("load_returned_true", view.load(deck))

    def poll():
        if time.monotonic() > deadline:
            state["err"] = f"timeout (status: {view._status.text()!r})"
            app.exit(1)
            return
        if view._web is None:
            QTimer.singleShot(500, poll)
            return

        def got(result):
            if result and "|" in result:
                xrefs, sections = (int(x) for x in result.split("|"))
                if xrefs >= 3 and sections >= 1:
                    if state["phase"] == "load":
                        check("initial_render_xrefs", True, f"xrefs={xrefs}")
                        check("engine_label", view.engine_label == "Marp",
                              view.engine_label)
                        state["phase"] = "reload"
                        view.reload()  # exercises hash capture + re-render
                        QTimer.singleShot(1500, poll)
                        return
                    check("after_reload_xrefs", True, f"xrefs={xrefs}")
                    app.exit(0)
                    return
            QTimer.singleShot(500, poll)

        try:
            view._native_eval_js_result(probe, got)
        except Exception as exc:  # keep polling; the deadline reports
            print("  probe error:", exc)
            QTimer.singleShot(500, poll)

    QTimer.singleShot(500, poll)
    app.exec()

    if state["err"]:
        check("e2e", False, state["err"])
    elif state["phase"] == "load":
        check("initial_render_xrefs", False, "never reached")

    view.cleanup()
    check("marp_watcher_stopped", view._proc is None)
    win.close()
    del os.environ["IDA_SLIDES_WEBVIEW2_UDF"]
    shutil.rmtree(tmp, ignore_errors=True)
    shutil.rmtree(udf, ignore_errors=True)
    return None


def main() -> int:
    if sys.platform != "win32":
        print("SKIP: Windows only (the macOS renderer needs IDA/PyObjC)")
        return 0

    print("Part 1: webview2_com smoke (bare Win32, no Qt)")
    com_smoke()

    print("Part 2: DeckWebView2View + marp E2E (Qt)")
    skip = qt_e2e()
    if skip:
        print(f"  SKIP: {skip}")

    failed = [n for n, ok in _results if not ok]
    print(f"\n{len(_results) - len(failed)} passed, {len(failed)} failed"
          + (f": {failed}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
