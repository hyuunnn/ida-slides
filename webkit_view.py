"""True-Marp slide renderer for macOS: embeds a native WKWebView inside the
IDA dock widget via PyObjC.

IDA's bundled PySide6 has no QtWebEngine, so this renderer embeds a native
WKWebView. The deck pipeline (marp CLI / slidev dev server, preprocessing,
status handling) lives in deck_view.DeckViewBase; this module implements only
the WKWebView-specific hook surface.

EMBEDDING: hand the WKWebView's NSView to Qt (QWindow.fromWinId +
QWidget.createWindowContainer) — never addSubview_ it into winId()'s NSView.
A raw subview is invisible to Qt, and inside IDA's dock its layer never joins
the screen compositor: the deck renders correctly (a WKWebView snapshot is
pixel-perfect) but the pane shows stale pixels of whatever was behind it,
and NO programmatic fix reconnects it (native frame nudge, Qt widget resize,
splitter, main-window resize, delayed attach all fail) — only resizing the
dock BY HAND. Letting Qt own the native window fixes it at the root.

CRASH SAFETY: a Python exception escaping a PyObjC delegate method becomes an
ObjC exception that unwinds through WebKit and aborts IDA. Worse, PyObjC
cannot call WebKit completion-handler *blocks* ("cannot call block without a
signature"), and WebKit aborts the process if a decision handler is dropped.
So this module never implements any delegate method that receives a block:

- @name click routing uses a WKScriptMessageHandler (postMessage from a JS
  click interceptor installed as a WKUserScript) — no blocks involved.
- The navigation delegate implements only webView:didFinishNavigation:
  (block-free) for slide-position restore after reloads.
- All IDA work is deferred out of ObjC callbacks via QTimer.singleShot(0).
"""

import logging
import os
import sys

from PySide6.QtCore import QTimer
from PySide6.QtGui import QWindow
from PySide6.QtWidgets import QVBoxLayout, QWidget

import deck_view

logger = logging.getLogger(__name__)


def webkit_available() -> bool:
    if sys.platform != "darwin":
        return False
    try:
        import objc  # noqa: F401
        import WebKit  # noqa: F401
    except ImportError:
        return False
    return True


_classes = None


def _make_objc_classes():
    """Create the ObjC helper classes lazily and only once per process."""
    global _classes
    if _classes is not None:
        return _classes

    import AppKit
    import objc

    # the ObjC runtime forbids redefining a class name; after a module
    # reload (plugin update, dev iteration) reuse the already-registered
    # classes. The V-suffix is bumped whenever their implementation changes
    # so a plugin upgrade in a running IDA still gets the new behavior.
    try:
        _classes = (
            objc.lookUpClass("IdaSlidesMsgHandlerV6"),
            objc.lookUpClass("IdaSlidesNavDelegateV6"),
        )
        return _classes
    except objc.nosuchclass_error:
        pass

    class IdaSlidesMsgHandlerV6(AppKit.NSObject):
        """WKScriptMessageHandler — plain args, no blocks. Never raises."""

        def setOwner_(self, owner):
            self._owner = owner

        def userContentController_didReceiveScriptMessage_(self, ucc, message):
            try:
                owner = getattr(self, "_owner", None)
                body = message.body()
                if owner is not None and body is not None:
                    # dispatch defers all IDA work via singleShot and
                    # swallows everything — nothing may escape into ObjC
                    deck_view.dispatch_page_message(owner, body)
            except Exception:
                logger.exception("script message handler failed")

    class IdaSlidesNavDelegateV6(AppKit.NSObject):
        """Implements ONLY the block-free didFinish callback."""

        def setOwner_(self, owner):
            self._owner = owner

        def webView_didFinishNavigation_(self, webview, nav):
            try:
                owner = getattr(self, "_owner", None)
                if owner is not None:
                    QTimer.singleShot(0, owner.on_load_finished)
            except Exception:
                logger.exception("didFinishNavigation handler failed")

    _classes = (IdaSlidesMsgHandlerV6, IdaSlidesNavDelegateV6)
    return _classes


class DeckWebKitView(deck_view.DeckViewBase):
    """deck_view.DeckViewBase over a native WKWebView (macOS + PyObjC)."""

    _ATTACH_FAIL_MSG = "WKWebView attach failed — see Output window"
    renderer_label = "WebKit"

    def __init__(self, parent: QWidget | None = None):
        self._delegate = None
        self._msg_handler = None
        self._ucc = None
        self._qwindow = None   # QWindow wrapping the WKWebView's NSView
        self._holder = None    # the QWidget Qt builds around it
        super().__init__(parent)

    # ------------------------------------------------------------------
    # native hook surface
    # ------------------------------------------------------------------
    def _native_attach(self) -> None:
        import AppKit
        import objc
        import WebKit

        msg_cls, nav_cls = _make_objc_classes()

        conf = WebKit.WKWebViewConfiguration.alloc().init()
        ucc = conf.userContentController()
        self._msg_handler = msg_cls.alloc().init()
        self._msg_handler.setOwner_(self)
        ucc.addScriptMessageHandler_name_(self._msg_handler, "ida")
        script = WebKit.WKUserScript.alloc().initWithSource_injectionTime_forMainFrameOnly_(
            deck_view.USER_JS, WebKit.WKUserScriptInjectionTimeAtDocumentEnd, True
        )
        ucc.addUserScript_(script)
        self._ucc = ucc

        w = max(self._container.width(), 1)
        h = max(self._container.height(), 1)
        web = WebKit.WKWebView.alloc().initWithFrame_configuration_(
            AppKit.NSMakeRect(0, 0, w, h), conf
        )
        self._delegate = nav_cls.alloc().init()
        self._delegate.setOwner_(self)
        web.setNavigationDelegate_(self._delegate)  # weak ref; we hold it

        # Embed through Qt's native-window container, NOT by poking
        # addSubview_ into winId()'s NSView. A raw subview is invisible to
        # Qt, and inside IDA's dock its layer never joins the screen
        # compositor: the deck renders (a WKWebView snapshot is perfect) but
        # the pane shows stale pixels of whatever was behind it, until the
        # dock is resized BY HAND — no programmatic resize (native frame, Qt
        # widget, splitter, main window) reconnects it. Handing the NSView to
        # Qt as a QWindow makes Qt own its geometry and compositing, which is
        # what actually puts it on screen.
        ptr = objc.pyobjc_id(web)
        qwin = QWindow.fromWinId(ptr)
        holder = QWidget.createWindowContainer(qwin, self._container)
        layout = QVBoxLayout(self._container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(holder)

        self._qwindow = qwin
        self._holder = holder
        self._web = web
        self._attach_done()

    def _native_teardown(self) -> None:
        if self._ucc is not None:
            try:
                self._ucc.removeScriptMessageHandlerForName_("ida")
            except Exception:
                logger.exception("message handler teardown failed")
            self._ucc = None
        if self._msg_handler is not None:
            try:
                self._msg_handler.setOwner_(None)
            except Exception:
                pass
        if self._web is not None:
            try:
                self._web.setNavigationDelegate_(None)
            except Exception:
                logger.exception("webview teardown failed")
        # Qt owns the native view through the container: dropping the holder
        # takes the QWindow (and the WKWebView's NSView) down with it
        if self._holder is not None:
            try:
                self._holder.setParent(None)
                self._holder.deleteLater()
            except Exception:
                logger.exception("window container teardown failed")
        self._holder = None
        self._qwindow = None
        self._delegate = None
        self._msg_handler = None

    def _native_load_url(self, url: str) -> None:
        if self._web is None:
            return
        try:
            import AppKit

            nsurl = AppKit.NSURL.URLWithString_(url)
            req = AppKit.NSURLRequest.requestWithURL_(nsurl)
            self._web.loadRequest_(req)
        except Exception:
            logger.exception("loadRequest failed for %s", url)

    def _native_load_file(self, path: str) -> None:
        if self._web is None:
            return
        try:
            import AppKit

            url = AppKit.NSURL.fileURLWithPath_(os.path.abspath(path))
            root = AppKit.NSURL.fileURLWithPath_(
                os.path.dirname(os.path.abspath(path))
            )
            self._web.loadFileURL_allowingReadAccessToURL_(url, root)
        except Exception:
            logger.exception("loadFileURL failed")

    def _native_eval_js(self, js: str) -> None:
        if self._web is None:
            return
        self._web.evaluateJavaScript_completionHandler_(js, None)

    def _native_eval_js_result(self, js: str, cb) -> None:
        if self._web is None:
            return

        def _completed(result, _error) -> None:
            # PyObjC completion block — must never raise, and must not do
            # IDA work inline: defer per the module crash-safety rules
            try:
                value = result if isinstance(result, str) and result else None
                QTimer.singleShot(0, lambda: cb(value))
            except Exception:
                logger.exception("JS completion failed")

        self._web.evaluateJavaScript_completionHandler_(js, _completed)

    def _native_focus_web(self) -> None:
        # the WKWebView is a native NSView that Qt does not track as a
        # focus child, so its first-responder status needs a nudge
        if self._web is not None:
            win = self._web.window()
            if win is not None:
                win.makeFirstResponder_(self._web)
