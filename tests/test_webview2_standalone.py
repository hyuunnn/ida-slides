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
QApplication + DeckWebView2View + the real marp -w pipeline, four phases:
initial render (tool discovery, render-complete detection, USER_JS
linkification), plain reload() on an unchanged deck (the _run_marp
`not fresh and not changed` fast path — a document tag must vanish, so a
silently no-op reload cannot pass), the save cycle (on_source_changed →
reload with hash capture → re-render, proven by a content MARKER), and a
rapid double save (latest content must win — the stale-render latch
regression, mirroring the macOS harness).

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
_TMP_PREFIXES = ("ida-slides-test-udf-", "ida-slides-wv2-", "ida-slides-e2e-")


def _private_udf() -> str:
    return tempfile.mkdtemp(prefix=_TMP_PREFIXES[0])


def _sweep_stale_test_dirs() -> None:
    """Reclaim temp dirs earlier runs could not delete (WebView2's browser
    shuts down asynchronously, so a profile folder can outlive its run)."""
    tmp = tempfile.gettempdir()
    for entry in os.listdir(tmp):
        if entry.startswith(_TMP_PREFIXES):
            shutil.rmtree(os.path.join(tmp, entry), ignore_errors=True)


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
        # exercise the two slots whose first-ever call would otherwise
        # happen live inside IDA (@token focus restore / dock drag) — a
        # wrong slot index fails HERE instead of crashing IDA
        ctrl.move_focus()
        ctrl.notify_parent_window_position_changed()
        check("focus_and_notify_slots", True)
        web = ctrl.get_core_webview2()
        if not check("get_core_webview2", web is not None):
            return
        state["web"] = web
        web.add_script_to_execute_on_document_created("window.__injected = 42;")
        web.add_web_message_received(lambda j: state["msgs"].append(j))
        web.add_navigation_completed(on_nav)
        web.add_new_window_requested(
            lambda u, suppressed: state["newwin"].append((u, suppressed))
        )
        web.navigate("file:///" + test_html.replace("\\", "/"))

    def on_nav(ok):
        if not ok:
            return
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
    # `suppressed` carries put_Handled's HRESULT — the event firing alone
    # would not prove the popup was actually blocked
    check(
        "new_window_suppressed",
        any("example.com" in u and ok for u, ok in state["newwin"]),
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
    # regression check for the ComCallback refcount model: after Close has
    # released the event handlers and the completions have run, no Python
    # COM object may remain referenced — a growing _LIVE is the leak that
    # once pinned every closed view for the life of the IDA session
    drain_deadline = time.monotonic() + 3
    while time.monotonic() < drain_deadline and wv2._LIVE:
        while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        time.sleep(0.01)
    check("com_handlers_drained", not wv2._LIVE, f"live={len(wv2._LIVE)}")
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

    # a content MARKER distinguishes DOM generations: the save phase must
    # observe the NEW marker, so a reload that silently does nothing (e.g.
    # a dropped hash-capture completion) fails instead of re-matching the
    # untouched pre-reload DOM
    def write_deck(marker: str) -> None:
        with open(deck, "w", encoding="utf-8") as f:
            f.write(
                f"---\nmarp: true\n---\n\n# Analysis of @main {marker}\n\n"
                "see @sub_401000 and @main:12\n\n---\n\n# Slide 2\n"
            )

    write_deck("MARKER_V0")

    app = QApplication.instance() or QApplication(sys.argv)
    win = QMainWindow()
    view = webview2_view.DeckWebView2View()
    win.setCentralWidget(view)
    win.resize(900, 700)
    win.show()

    state = {"phase": "load", "err": None}
    deadline = time.monotonic() + 150
    # marker + 3 @tokens + at least one bespoke svg/section per slide +
    # the __pre_reload flag (survives only in the pre-reload DOM, so its
    # disappearance proves the unchanged-content reload really renavigated)
    probe = (
        "((document.body.textContent.match(/MARKER_V\\d+/)||[''])[0]) + '|' + "
        "document.querySelectorAll('a.ida-xref').length + '|' + "
        "document.querySelectorAll('svg,section').length + '|' + "
        "(window.__pre_reload||0)"
    )

    check("load_returned_true", view.load(deck))

    def got(result):
        # runs on ExecuteScript completion; everything guarded so a bad
        # probe result can never kill the poll loop (the repeating timer
        # keeps ticking regardless). Phase transitions are one-way, so
        # duplicate/straggler completions match no branch.
        try:
            if not result or result.count("|") != 3:
                return
            marker, xrefs_s, sections_s, pre_s = result.split("|")
            xrefs, sections, pre = int(xrefs_s), int(sections_s), int(pre_s)
            if xrefs < 3 or sections < 1:
                return
            if state["phase"] == "load":
                check("initial_render_xrefs", True, f"xrefs={xrefs}")
                check("engine_label", view.engine_label == "Marp",
                      view.engine_label)
                # phase 2: plain reload() on an UNCHANGED deck — the
                # _run_marp `not fresh and not changed` fast path (the R
                # shortcut). Tag the current document first; the tag not
                # surviving proves a real renavigation happened.
                state["phase"] = "flagging"

                def _flagged(r):
                    if r == "ok" and state["phase"] == "flagging":
                        state["phase"] = "reload"
                        view.reload()

                view._native_eval_js_result(
                    "window.__pre_reload = 1; 'ok'", _flagged
                )
            elif state["phase"] == "reload" and pre == 0:
                # fresh document, same content generation: the fast path
                # renavigated (a silently no-op reload keeps pre == 1)
                check("unchanged_reload_fastpath", True)
                state["phase"] = "save"
                # phase 3: the save cycle a real editor save takes
                # (on_source_changed → reload → hash capture → re-render);
                # MARKER_V1 in the DOM proves the new render
                write_deck("MARKER_V1")
                view.on_source_changed()
            elif state["phase"] == "save" and marker == "MARKER_V1":
                check("save_rerendered", True, f"xrefs={xrefs}")
                # phase 4: rapid double save — the SECOND save lands while
                # the first render is (or may be) in flight; the latest
                # content must win (regression for the stale-render latch,
                # cf. the macOS harness's double-save phase)
                state["phase"] = "doublesave_pending"
                write_deck("MARKER_V2")
                view.on_source_changed()

                def _second_save():
                    write_deck("MARKER_V3")
                    view.on_source_changed()
                    state["phase"] = "doublesave"

                QTimer.singleShot(250, _second_save)
            elif state["phase"] == "doublesave" and marker == "MARKER_V3":
                check("double_save_latest_wins", True)
                state["phase"] = "done"
                app.exit(0)
        except Exception as exc:
            print("  probe parse error:", exc)

    def poll():
        # driven by a repeating timer: a wedged webview whose ExecuteScript
        # completion never arrives still hits the deadline and FAILS,
        # instead of hanging app.exec() forever
        if time.monotonic() > deadline:
            state["err"] = (
                f"timeout in phase {state['phase']!r} "
                f"(status: {view._status.text()!r})"
            )
            app.exit(1)
            return
        if view._web is None:
            if getattr(view, "attach_failed", False):
                state["err"] = "attach failed"
                app.exit(1)
            return
        try:
            view._native_eval_js_result(probe, got)
        except Exception as exc:
            print("  probe error:", exc)

    ticker = QTimer()
    ticker.timeout.connect(poll)
    ticker.start(600)
    app.exec()
    ticker.stop()

    # single exit gate: anything short of "done" is a failure, including
    # phases added in the future — no per-phase ladder to keep in sync
    if state["phase"] != "done":
        check("e2e", False,
              state["err"] or f"stopped in phase {state['phase']!r}")

    view.cleanup()
    check("marp_watcher_stopped", view._proc is None)
    win.close()

    # give the browser's post-Close handler releases a chance to land (the
    # pump is gone once app.exec() returns), then drop the shared
    # environment so the browser exits and the temp profile can actually
    # be deleted — otherwise one UDF folder leaks per run
    import webview2_com as wv2

    drain_deadline = time.monotonic() + 3
    while time.monotonic() < drain_deadline and wv2._LIVE:
        app.processEvents()
        time.sleep(0.01)
    print(f"  info: _LIVE after e2e teardown: {len(wv2._LIVE)}")
    if webview2_view._shared_env is not None:
        webview2_view._shared_env.release()
        webview2_view._shared_env = None

    del os.environ["IDA_SLIDES_WEBVIEW2_UDF"]
    shutil.rmtree(tmp, ignore_errors=True)
    # browser shutdown is async — retry briefly, and the startup sweep in
    # main() reclaims anything a slow shutdown still holds this run
    for _ in range(15):
        shutil.rmtree(udf, ignore_errors=True)
        if not os.path.isdir(udf):
            break
        app.processEvents()
        time.sleep(0.4)
    return None


def main() -> int:
    if sys.platform != "win32":
        print("SKIP: Windows only (the macOS renderer needs IDA/PyObjC)")
        return 0

    _sweep_stale_test_dirs()

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
