"""True-Marp slide renderer for Windows: embeds a native WebView2 control
inside the IDA dock widget over ctypes COM (webview2_com).

IDA's bundled PySide6 has no QtWebEngine on Windows either, so — mirroring
the macOS WKWebView decision — the system WebView2 runtime (evergreen,
ships with Windows 10/11) is attached to the Qt container's HWND. The deck
pipeline lives in deck_view.DeckViewBase; this module implements only the
WebView2-specific hook surface.

Async shape: WebView2 creation is a two-step callback chain (environment →
controller), so unlike WKWebView the attach completes asynchronously; the
base class already defers any pre-attach load() until _attach_done().

CRASH SAFETY (see webview2_com): COM callbacks never raise, and all IDA
work is deferred out of callback frames via QTimer.singleShot(0) —
dispatch_page_message does this internally.
"""

import ctypes
import json
import logging
import os
import shutil
from ctypes import wintypes

from PySide6.QtCore import QEvent, QTimer, QUrl
from PySide6.QtWidgets import QWidget

import deck_view
import webview2_com

logger = logging.getLogger(__name__)

_user32 = ctypes.WinDLL("user32")

_LOADER_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "win", "WebView2Loader.dll"
)

# one browser environment per process: environments with the same user-data
# folder are shareable, and reusing it makes reopening the form instant
_shared_env: "webview2_com.WebView2Environment | None" = None


def _user_data_dir() -> str:
    # override hook for tests (and users who want the cache elsewhere)
    override = os.environ.get("IDA_SLIDES_WEBVIEW2_UDF")
    if override:
        return override
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "ida-slides", "webview2-data")


def _pid_running(pid: int) -> bool:
    kernel32 = ctypes.WinDLL("kernel32")
    h = kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
    if not h:
        return False
    kernel32.CloseHandle(h)
    return True


def _sweep_stale_udfs() -> None:
    """Remove per-pid user-data folders (see _controller_ready) whose IDA
    process is gone — they are caches, never precious."""
    base = _user_data_dir()
    parent, prefix = os.path.dirname(base), os.path.basename(base) + "-"
    try:
        for entry in os.listdir(parent):
            if not entry.startswith(prefix):
                continue
            pid_part = entry[len(prefix):]
            if pid_part.isdigit() and not _pid_running(int(pid_part)):
                shutil.rmtree(os.path.join(parent, entry), ignore_errors=True)
    except OSError:
        pass


def availability_error() -> str | None:
    """None when WebView2 can render here, else a user-facing reason."""
    if not os.path.isfile(_LOADER_PATH):
        return (
            "ida-slides: win/WebView2Loader.dll is missing from the plugin "
            "directory.\n\nRe-install the plugin, or copy the x64 loader "
            "from the Microsoft.Web.WebView2 NuGet package."
        )
    try:
        # distinguish "the DLL itself would not load" (ARM64 host running
        # an x64 loader, antivirus block) from "no runtime installed" —
        # the runtime-install advice is useless for the former
        webview2_com._load_loader(_LOADER_PATH)
    except OSError as exc:
        return (
            "ida-slides: win/WebView2Loader.dll exists but could not be "
            f"loaded ({exc}).\n\nThe shipped loader is x64-only (see "
            "win/PROVENANCE.txt); an ARM64 IDA needs the win-arm64 loader, "
            "and antivirus software can also block DLL loads."
        )
    if webview2_com.runtime_version(_LOADER_PATH) is None:
        return (
            "ida-slides: the WebView2 Runtime is not installed.\n\n"
            "Install it from "
            "https://developer.microsoft.com/microsoft-edge/webview2/"
        )
    return None


class DeckWebView2View(deck_view.DeckViewBase):
    """deck_view.DeckViewBase over a native WebView2 control (Windows)."""

    _ATTACH_FAIL_MSG = "WebView2 attach failed — see Output window"
    renderer_label = "WebView2"

    #: how long one environment→controller attempt may take before the
    #: fallback kicks in. Normal attach is well under a second; a stall
    #: means the shared user-data folder is wedged (see _attempt_attach).
    _ATTACH_TIMEOUT_MS = 8000

    def __init__(self, parent: QWidget | None = None):
        self._controller: webview2_com.WebView2Controller | None = None
        self._webview: webview2_com.WebView2 | None = None
        self._closing = False
        # generation counter: every attach attempt gets its own number, and
        # callbacks from superseded attempts (a stalled one can complete
        # AFTER its timeout already started the retry) are dropped on
        # arrival instead of racing the live attempt
        self._attach_gen = 0
        super().__init__(parent)
        # WebView2 does not autoresize with its parent HWND — track the
        # container widget instead
        self._container.installEventFilter(self)

    # ------------------------------------------------------------------
    # native hook surface
    # ------------------------------------------------------------------
    def _native_attach(self) -> None:
        err = availability_error()
        if err is not None:
            raise RuntimeError(err.splitlines()[0])
        _sweep_stale_udfs()
        self._attempt_attach(_user_data_dir(), retries_left=1)

    def _attempt_attach(self, udf: str, retries_left: int) -> None:
        """One environment→controller attempt against `udf`.

        Sharing a user-data folder with another host process's browser
        instance (a second IDA, or a leftover mid-shutdown one) makes
        CreateCoreWebView2Controller fail with ERROR_INVALID_STATE
        (0x8007139f) — or, observed empirically, simply never complete.
        Both surface here the same way: the attempt is abandoned (fast
        failure, or the watchdog below) and retried once with a
        per-process folder; _sweep_stale_udfs reclaims those later.
        """
        self._attach_gen += 1
        gen = self._attach_gen
        hwnd = int(self._container.winId())

        # watchdog for the never-completes case; harmless when the attach
        # succeeds (self._web set) or already moved on (gen mismatch)
        QTimer.singleShot(
            self._ATTACH_TIMEOUT_MS,
            lambda: self._on_attach_timeout(gen, udf, retries_left),
        )

        def _on_ctrl(ctrl, hr) -> None:
            QTimer.singleShot(
                0, lambda: self._controller_ready(ctrl, hr, gen, udf,
                                                  retries_left)
            )

        if _shared_env is not None:
            _shared_env.create_controller(hwnd, _on_ctrl)
            return

        def _on_env(env, hr) -> None:
            # escape the COM callback frame before doing anything further
            QTimer.singleShot(
                0, lambda: self._env_ready(env, hr, hwnd, gen, udf,
                                           retries_left)
            )

        webview2_com.create_environment(_LOADER_PATH, udf, _on_env)

    def _on_attach_timeout(self, gen: int, udf: str, retries_left: int) -> None:
        if gen != self._attach_gen or self._web is not None or self._closing:
            return
        logger.warning("WebView2 attach timed out on %s", udf)
        self._retry_or_fail(udf, retries_left)

    def _retry_or_fail(self, udf: str, retries_left: int) -> None:
        global _shared_env
        if _shared_env is not None:
            # the stalled/failed env must not be reused — and our reference
            # must be RELEASED, or its browser-process tree outlives us and
            # keeps the user-data folder locked (a late controller callback
            # on it is handled by the gen-mismatch drop, so this is safe)
            _shared_env.release()
            _shared_env = None
        if retries_left > 0 and not self._closing:
            private = f"{_user_data_dir()}-{os.getpid()}"
            logger.warning("retrying WebView2 with private folder %s", private)
            self._attempt_attach(private, retries_left - 1)
        else:
            # invalidate the generation: a stalled final attempt completing
            # AFTER this failure verdict must be dropped by the gen guards,
            # not attach into a view whose attach_failed flag is latched
            # (and the leftover watchdog for it must not re-run this path)
            self._attach_gen += 1
            self._attach_failed(self._ATTACH_FAIL_MSG)

    def _env_ready(
        self, env, hr: int, hwnd: int, gen: int, udf: str, retries_left: int
    ) -> None:
        global _shared_env
        if gen != self._attach_gen or self._closing:
            if env is not None:
                env.release()  # superseded attempt — drop its environment
            return
        if env is None:
            logger.error("WebView2 environment failed: 0x%08x", hr & 0xFFFFFFFF)
            self._retry_or_fail(udf, retries_left)
            return
        _shared_env = env

        def _on_ctrl(ctrl, hr2) -> None:
            QTimer.singleShot(
                0, lambda: self._controller_ready(ctrl, hr2, gen, udf,
                                                  retries_left)
            )

        env.create_controller(hwnd, _on_ctrl)

    def _controller_ready(
        self, ctrl, hr: int, gen: int, udf: str, retries_left: int
    ) -> None:
        if gen != self._attach_gen or self._closing:
            if ctrl is not None:  # superseded attempt — drop its controller
                ctrl.close()
                ctrl.release()
            return
        if ctrl is None:
            logger.error("WebView2 controller failed: 0x%08x", hr & 0xFFFFFFFF)
            self._retry_or_fail(udf, retries_left)
            return
        web = ctrl.get_core_webview2()
        if web is None:
            ctrl.close()
            ctrl.release()
            self._attach_failed(self._ATTACH_FAIL_MSG)
            return
        self._controller = ctrl
        self._webview = web
        web.add_script_to_execute_on_document_created(deck_view.USER_JS)
        web.add_web_message_received(self._on_web_message)
        # success-only, matching WKWebView's didFinishNavigation: an aborted
        # navigation (superseded by a newer save's navigate) also fires
        # NavigationCompleted, and letting it through would consume
        # _pending_hash — snapping the deck off the current slide
        web.add_navigation_completed(
            lambda ok: ok and QTimer.singleShot(0, self.on_load_finished)
        )
        # suppress popups; USER_JS already routes external links, this only
        # catches window.open / middle-click paths it can't intercept
        web.add_new_window_requested(
            lambda uri, _suppressed: QTimer.singleShot(
                0, lambda u=uri: deck_view._open_external(u)
            )
        )
        self._sync_bounds()
        ctrl.put_is_visible(True)
        self._web = web  # the base's "native view ready" flag
        self._attach_done()

    def _on_web_message(self, message_json: str) -> None:
        # COM callback frame: parse only, then hand off (dispatch defers)
        try:
            body = json.loads(message_json)
        except ValueError:
            logger.debug("undecodable web message: %r", message_json[:200])
            return
        if isinstance(body, dict):
            deck_view.dispatch_page_message(self, body)

    def _native_teardown(self) -> None:
        self._closing = True
        if self._controller is not None:
            self._controller.close()
            self._controller.release()
            self._controller = None
        if self._webview is not None:
            self._webview.release()
            self._webview = None
        # the shared environment is deliberately kept for the next view

    def _native_load_url(self, url: str) -> None:
        if self._webview is not None:
            self._webview.navigate(url)

    def _native_load_file(self, path: str) -> None:
        if self._webview is not None:
            self._webview.navigate(
                QUrl.fromLocalFile(os.path.abspath(path)).toString()
            )

    def _native_eval_js(self, js: str) -> None:
        if self._webview is not None:
            self._webview.execute_script(js)

    def _native_eval_js_result(self, js: str, cb) -> None:
        if self._webview is None:
            return

        def _done(result_json: str | None) -> None:
            # ExecuteScript results arrive JSON-encoded ('"#3"', 'null')
            value = None
            if result_json:
                try:
                    decoded = json.loads(result_json)
                except ValueError:
                    decoded = None
                if isinstance(decoded, str) and decoded:
                    value = decoded
            QTimer.singleShot(0, lambda: cb(value))

        self._webview.execute_script(js, _done)

    def _native_focus_web(self) -> None:
        if self._controller is not None:
            self._controller.move_focus()

    # ------------------------------------------------------------------
    # geometry
    # ------------------------------------------------------------------
    def eventFilter(self, obj, event) -> bool:
        if obj is self._container and self._controller is not None:
            t = event.type()
            if t in (QEvent.Type.Resize, QEvent.Type.Show):
                self._sync_bounds()
            elif t == QEvent.Type.Move:
                self._controller.notify_parent_window_position_changed()
        return super().eventFilter(obj, event)

    def _sync_bounds(self) -> None:
        if self._controller is None:
            return
        try:
            hwnd = int(self._container.winId())
            rect = wintypes.RECT()
            if _user32.GetClientRect(hwnd, ctypes.byref(rect)):
                self._controller.put_bounds(0, 0, rect.right, rect.bottom)
        except Exception:
            logger.exception("bounds sync failed")
