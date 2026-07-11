"""True-Marp slide renderer for macOS: embeds a native WKWebView inside the
IDA dock widget via PyObjC.

IDA's bundled PySide6 has no QtWebEngine, so this renderer attaches a native
WKWebView as a subview of the Qt widget's NSView (winId). Markdown decks are
converted with the real marp CLI on every save, giving pixel-perfect Marp
theme rendering; marp-cli HTML output can also be opened directly.

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

import glob
import json
import logging
import os
import re
import shutil
import socket
import sys

from PySide6.QtCore import QProcess, QProcessEnvironment, Qt, QTimer
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

import ida_links

logger = logging.getLogger(__name__)

_NODE_BIN_GLOBS = [
    os.path.expanduser("~/.nvm/versions/node/*/bin"),
    "/opt/homebrew/bin",
    "/usr/local/bin",
]

# front-matter keys that identify a Slidev deck (none of these are Marp's)
_SLIDEV_FM_KEYS = {
    "transition", "mdc", "drawings", "highlighter", "monaco", "colorSchema",
    "routerMode", "canvasWidth", "aspectRatio", "fonts", "addons",
    "titleTemplate", "presenter", "browserExporter", "htmlAttrs",
    "lineNumbers", "record", "selectable", "seoMeta", "favicon", "info",
}

# Injected into every page load: linkify @tokens, intercept clicks, and relay
# them to Python over the "ida" message channel. Marp output is a static DOM,
# but Slidev is a Vue SPA that mounts slides dynamically, so linkification
# re-runs through a MutationObserver (disconnected during our own rewrites to
# avoid feedback loops).
USER_JS = r"""
(function () {
    if (window.__idaPptHooked) return;
    window.__idaPptHooked = true;

    var RE = __IDA_TOKEN_RE__;

    function addStyle() {
        if (!document.head || document.getElementById('ida-xref-style')) return;
        var style = document.createElement('style');
        style.id = 'ida-xref-style';
        style.textContent =
            'a.ida-xref{color:#4ea1ff;background:rgba(78,161,255,.15);' +
            'border-radius:3px;padding:0 .15em;text-decoration:none;' +
            'font-family:monospace;cursor:pointer;}' +
            'a.ida-xref:hover{background:rgba(78,161,255,.35);}' +
            '.ida-tip{position:fixed;z-index:2147483647;max-width:64ch;' +
            'background:#1d232f;color:#c9d4e4;border:1px solid #3a4558;' +
            'border-radius:6px;padding:8px 10px;font-family:ui-monospace,' +
            'Menlo,monospace;font-size:12px;line-height:1.45;' +
            'white-space:pre;overflow:hidden;box-shadow:0 4px 14px ' +
            'rgba(0,0,0,.45);pointer-events:none;}';
        document.head.appendChild(style);
    }

    function linkify(root) {
        if (!root) return;
        var walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
        var targets = [];
        while (walker.nextNode()) {
            var n = walker.currentNode;
            if (n.parentElement &&
                n.parentElement.closest('a,script,style,textarea,[contenteditable]'))
                continue;
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
                return '<a class="ida-xref" data-ida-name="' + name + '"' +
                    (line ? ' data-ida-line="' + line + '"' : '') + '>' +
                    label + '</a>' + trail;
            });
            n.parentNode.replaceChild(span, n);
        });
        return targets.length;
    }

    var observer = null;
    var scheduled = false;

    function rescan() {
        scheduled = false;
        if (observer) observer.disconnect();
        addStyle();
        linkify(document.body);
        if (observer && document.body)
            observer.observe(document.body,
                             {childList: true, subtree: true, characterData: true});
    }

    function schedule() {
        if (scheduled) return;
        scheduled = true;
        setTimeout(rescan, 150);
    }

    observer = new MutationObserver(schedule);
    rescan();

    // ---- hover preview: pseudocode excerpt tooltip ------------------------
    var previewCache = {};
    var previewSeq = 0;
    var hoverEl = null;
    var hoverTimer = null;
    var tip = null;

    var mouseX = 0, mouseY = 0;
    document.addEventListener('mousemove', function (ev) {
        mouseX = ev.clientX;
        mouseY = ev.clientY;
        // mouseout alone can't be trusted to close the tip: a keyboard
        // slide change hides/replaces the hovered link without firing it
        if (tip && tip.style.display === 'block' &&
            (!hoverEl || !hoverEl.contains(ev.target)))
            hideTip();
    }, true);
    document.addEventListener('keydown', function () { hideTip(); }, true);
    window.addEventListener('hashchange', function () { hideTip(); });

    function showTip(el, text) {
        if (!tip) {
            tip = document.createElement('div');
            tip.className = 'ida-tip';
        }
        if (!tip.isConnected) document.body.appendChild(tip);
        tip.textContent = text;
        tip.style.display = 'block';
        // anchor to the mouse cursor (falls back to the link's box if the
        // cursor position isn't known yet), offset so it doesn't sit under
        // the pointer
        var w = tip.offsetWidth, h = tip.offsetHeight;
        var x = (mouseX || el.getBoundingClientRect().left) + 14;
        var y = (mouseY || el.getBoundingClientRect().bottom) + 16;
        if (x + w > window.innerWidth - 6) x = window.innerWidth - w - 6;
        if (y + h > window.innerHeight - 6) y = (mouseY || 0) - h - 12;
        tip.style.left = Math.max(4, x) + 'px';
        tip.style.top = Math.max(4, y) + 'px';
    }

    function hideTip() {
        if (tip) tip.style.display = 'none';
        if (hoverTimer) { clearTimeout(hoverTimer); hoverTimer = null; }
        hoverEl = null;   // also blocks a late preview reply from re-showing
    }

    window.__idaSlidesPreview = function (id, key, text) {
        previewCache[key] = text;
        if (hoverEl && hoverEl.__idaReq === id && text)
            showTip(hoverEl, text);
    };

    document.addEventListener('mouseover', function (ev) {
        var a = ev.target && ev.target.closest ?
            ev.target.closest('a.ida-xref') : null;
        if (!a) return;
        hoverEl = a;
        if (hoverTimer) clearTimeout(hoverTimer);
        hoverTimer = setTimeout(function () {
            if (hoverEl !== a) return;
            var name = a.getAttribute('data-ida-name');
            var line = a.getAttribute('data-ida-line') || '';
            var key = name + ':' + line;
            if (previewCache[key] !== undefined) {
                if (previewCache[key]) showTip(a, previewCache[key]);
                return;
            }
            var id = String(++previewSeq);
            a.__idaReq = id;
            window.webkit.messageHandlers.ida.postMessage(
                {type: 'preview', name: name, line: line, id: id, key: key});
        }, 250);
    }, true);

    document.addEventListener('mouseout', function (ev) {
        var a = ev.target && ev.target.closest ?
            ev.target.closest('a.ida-xref') : null;
        if (!a) return;
        if (hoverTimer) clearTimeout(hoverTimer);
        if (hoverEl === a) hoverEl = null;
        hideTip();
    }, true);

    document.addEventListener('click', function (ev) {
        var t = ev.target;
        if (!t || !t.closest) return;
        var xref = t.closest('a.ida-xref');
        if (xref) {
            ev.preventDefault();
            ev.stopPropagation();
            hideTip();
            window.webkit.messageHandlers.ida.postMessage(
                {type: 'jump', name: xref.getAttribute('data-ida-name'),
                 line: xref.getAttribute('data-ida-line')});
            return;
        }
        var a = t.closest('a[href]');
        if (a && /^https?:/i.test(a.href) &&
            new URL(a.href, location.href).origin !== location.origin) {
            // external links open in the system browser; same-origin
            // navigation (e.g. Slidev's SPA routing) stays in the deck
            ev.preventDefault();
            ev.stopPropagation();
            window.webkit.messageHandlers.ida.postMessage(
                {type: 'ext', url: a.href});
        }
    }, true);
})();
""".replace("__IDA_TOKEN_RE__", ida_links.JS_TOKEN_RE)


def _find_node_tool(name: str) -> str | None:
    path = shutil.which(name)
    if path:
        return path
    for pattern in _NODE_BIN_GLOBS:
        hits = sorted(glob.glob(os.path.join(pattern, name)), reverse=True)
        if hits:
            return hits[0]
    return None


def find_marp() -> str | None:
    return _find_node_tool("marp")


def find_slidev() -> str | None:
    return _find_node_tool("slidev")


def _front_matter_lines(md_path: str) -> list[str]:
    try:
        with open(md_path, encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError:
        return []
    if not lines or lines[0].strip() != "---":
        return []
    for i in range(1, min(len(lines), 100)):
        if lines[i].strip() == "---":
            return lines[1:i]
    return []


def _yaml_scalar(raw: str) -> str:
    """Normalize a front-matter scalar: drop an unquoted inline comment and
    surrounding quotes so `marp: false # opt out` reads as `false`."""
    v = raw.strip()
    if v[:1] not in ("'", '"'):
        v = v.split("#", 1)[0].strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
        v = v[1:-1]
    return v


def detect_engine(md_path: str) -> str:
    """Pick the presentation engine for a Markdown deck: 'marp' or 'slidev'.

    Explicit `ida-slides-engine: <engine>` in the front matter wins; then
    `marp: true` selects Marp; then any Slidev-specific front-matter key
    selects Slidev (if its CLI is installed). Marp is the default.
    """
    fm = _front_matter_lines(md_path)
    keys = {}
    for line in fm:
        m = re.match(r"^([A-Za-z_-]+)\s*:\s*(.*)$", line)
        if m:
            keys[m.group(1)] = _yaml_scalar(m.group(2))

    override = keys.get("ida-slides-engine", "").lower()
    if override in ("marp", "slidev"):
        return override
    marp_val = keys.get("marp")
    if marp_val is not None and marp_val.lower() not in ("false", "no", "off", "0"):
        return "marp"
    if _SLIDEV_FM_KEYS & keys.keys() and find_slidev():
        return "slidev"
    return "marp"


def webkit_available() -> bool:
    if sys.platform != "darwin":
        return False
    try:
        import objc  # noqa: F401
        import WebKit  # noqa: F401
    except ImportError:
        return False
    return True


def _safe_jump(name: str, line: int | None = None) -> None:
    try:
        ida_links.jump_to(name, line)
    except Exception:
        logger.exception("jump failed for %s", name)


def _open_external(spec: str) -> None:
    try:
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices

        QDesktopServices.openUrl(QUrl(spec))
    except Exception:
        logger.exception("failed to open external url")


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
            objc.lookUpClass("IdaSlidesMsgHandlerV5"),
            objc.lookUpClass("IdaSlidesNavDelegateV5"),
        )
        return _classes
    except objc.nosuchclass_error:
        pass

    class IdaSlidesMsgHandlerV5(AppKit.NSObject):
        """WKScriptMessageHandler — plain args, no blocks. Never raises."""

        def setOwner_(self, owner):
            self._owner = owner

        def userContentController_didReceiveScriptMessage_(self, ucc, message):
            try:
                body = message.body()
                kind = str(body.get("type") or "")
                if kind == "jump":
                    owner = getattr(self, "_owner", None)
                    name = str(body.get("name") or "")
                    line_v = body.get("line")
                    try:
                        line = int(str(line_v)) if line_v else None
                    except (TypeError, ValueError):
                        line = None
                    if name and owner is not None:
                        QTimer.singleShot(
                            0, lambda o=owner, n=name, l=line: o.do_jump(n, l)
                        )
                elif kind == "preview":
                    owner = getattr(self, "_owner", None)
                    name = str(body.get("name") or "")
                    line_v = body.get("line")
                    try:
                        line = int(str(line_v)) if line_v else None
                    except (TypeError, ValueError):
                        line = None
                    req_id = str(body.get("id") or "")
                    key = str(body.get("key") or "")
                    if owner is not None and name:
                        QTimer.singleShot(
                            0,
                            lambda o=owner, n=name, l=line, r=req_id, k=key:
                                o.deliver_preview(n, l, r, k),
                        )
                elif kind == "ext":
                    url = str(body.get("url") or "")
                    if url:
                        QTimer.singleShot(0, lambda u=url: _open_external(u))
            except Exception:
                logger.exception("script message handler failed")

    class IdaSlidesNavDelegateV5(AppKit.NSObject):
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

    _classes = (IdaSlidesMsgHandlerV5, IdaSlidesNavDelegateV5)
    return _classes


class MarpWebKitView(QWidget):
    """Renders a Marp deck with a native WKWebView.

    .md files are converted via marp CLI to a hidden HTML file next to the
    source (so relative image paths keep working); .html files load directly.
    """

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._path: str | None = None            # file the user opened
        self._generated: str | None = None       # html we own and clean up
        self._generated_md: str | None = None    # preprocessed md we own
        self._pending_hash: str | None = None
        self._web = None
        self._delegate = None
        self._msg_handler = None
        self._ucc = None
        self._marp: str | None = None
        self._proc: QProcess | None = None       # long-lived `marp -w` watcher
        self._watch_key: tuple[str, str] | None = None
        self._pending_out: str | None = None      # html we're waiting on
        self._pending_restore: str | None = None  # hash to restore on that load
        self._render_timeout: QTimer | None = None
        self._last_marp_err: str | None = None    # last error line from marp
        self.engine_label = "Marp"
        self._form_caption = "ida-slides"

        # slidev dev-server state
        self._slidev_proc: QProcess | None = None
        self._slidev_md: str | None = None
        self._slidev_port: int | None = None
        self._poll_timer: QTimer | None = None
        self._poll_tries = 0

        self._status = QLabel("", self)
        self._status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._status.setWordWrap(True)
        self._status.setVisible(False)

        self._container = QWidget(self)
        self._container.setAttribute(Qt.WidgetAttribute.WA_NativeWindow, True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._status)
        layout.addWidget(self._container, 1)

        # attach after the widget is realized inside the dock
        QTimer.singleShot(0, self._attach_webview)

    # ------------------------------------------------------------------
    def _attach_webview(self) -> None:
        if self._web is not None:
            return
        try:
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
                USER_JS, WebKit.WKUserScriptInjectionTimeAtDocumentEnd, True
            )
            ucc.addUserScript_(script)
            self._ucc = ucc

            nsview = objc.objc_object(c_void_p=int(self._container.winId()))
            web = WebKit.WKWebView.alloc().initWithFrame_configuration_(
                nsview.bounds(), conf
            )
            web.setAutoresizingMask_(
                AppKit.NSViewWidthSizable | AppKit.NSViewHeightSizable
            )
            self._delegate = nav_cls.alloc().init()
            self._delegate.setOwner_(self)
            web.setNavigationDelegate_(self._delegate)  # weak ref; we hold it
            nsview.addSubview_(web)
            self._web = web
        except Exception:
            logger.exception("failed to attach WKWebView")
            self._show_status("WKWebView attach failed — see Output window")
            return

        if self._path:
            pending, self._path = self._path, None
            self.load(pending)

    def _show_status(self, text: str) -> None:
        self._status.setText(text)
        self._status.setVisible(bool(text))

    # ------------------------------------------------------------------
    # Public surface (same as the other renderers)
    # ------------------------------------------------------------------
    def load(self, path: str, restore_hash: str | None = None) -> None:
        self._path = path
        ext = os.path.splitext(path)[1].lower()
        is_md = ext in (".md", ".markdown")
        engine = detect_engine(path) if is_md else "marp"
        self.engine_label = "Slidev" if engine == "slidev" else "Marp"
        if self._web is None:
            return  # _attach_webview picks it up
        # clear any stale error banner (e.g. "marp watcher exited") so it
        # doesn't linger over a freshly loaded deck
        self._show_status("")
        if engine == "slidev":
            self._stop_marp()
            self._run_slidev(path)
        elif is_md:
            self._stop_slidev()
            self._run_marp(path, restore_hash)
        else:
            self._stop_slidev()
            self._stop_marp()
            self._load_html(path)

    def reload(self) -> None:
        if self._path is None or self._web is None:
            return
        if self._slidev_proc is not None:
            try:
                self._web.evaluateJavaScript_completionHandler_(
                    "window.location.reload()", None
                )
            except Exception:
                logger.exception("slidev reload failed")
            return

        # capture the current slide's hash, then reload the SAME deck and
        # restore it. Bind both to `path`: if the user switches decks while
        # the async capture is in flight, abandon the stale reload instead
        # of reloading the old deck or restoring its hash onto the new one.
        path = self._path

        def _captured(result, _error) -> None:
            try:
                if self._path != path:
                    return  # deck switched since reload started
                h = result if isinstance(result, str) and result else None
                self.load(path, restore_hash=h)
            except Exception:
                logger.exception("hash-capture completion failed")

        try:
            self._web.evaluateJavaScript_completionHandler_(
                "window.location.hash", _captured
            )
        except Exception:
            logger.exception("hash capture failed; reloading cold")
            self.load(path)

    def on_source_changed(self) -> None:
        """The source file was saved."""
        if self._slidev_proc is not None:
            # regenerate the preprocessed deck; Slidev's HMR picks it up
            if self._path:
                self._prepare_md(self._path)
            return
        self.reload()

    def cleanup(self) -> None:
        """Detach the native view; call before the Qt widget goes away."""
        self._stop_marp()
        self._stop_slidev()
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
                self._web.removeFromSuperview()
            except Exception:
                logger.exception("webview teardown failed")
            self._web = None
        self._delegate = None
        self._msg_handler = None
        self._remove_generated()

    # ------------------------------------------------------------------
    # deck preprocessing (@name[a:b] pseudocode embeds)
    # ------------------------------------------------------------------
    def _prepare_md(self, md_path: str) -> tuple[str, bool]:
        """Expand embed tokens into a hidden sibling md.

        Returns (path, changed): the sibling's path (or the original file on
        failure) and whether its content differs from the last render, so a
        caller can tell whether a re-render will actually happen. `changed`
        is returned per call rather than stored, so an early failure can't
        leak a stale flag into a later save.
        """
        try:
            import deck_preprocess

            with open(md_path, encoding="utf-8", errors="replace") as f:
                text = f.read()
            expanded = deck_preprocess.expand_embeds(text)
        except Exception:
            logger.exception("embed preprocessing failed for %s", md_path)
            return md_path, True  # unknown → assume a render is needed

        stem = os.path.splitext(os.path.basename(md_path))[0]
        out = os.path.join(os.path.dirname(md_path), f".{stem}.ida-slides.md")
        try:
            old = None
            if os.path.exists(out):
                with open(out, encoding="utf-8", errors="replace") as f:
                    old = f.read()
            changed = old != expanded
            if changed:
                with open(out, "w", encoding="utf-8") as f:
                    f.write(expanded)
        except OSError:
            logger.exception("cannot write preprocessed deck %s", out)
            return md_path, True
        self._generated_md = out
        return out, changed

    # ------------------------------------------------------------------
    # process helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _tool_env(tool_path: str) -> QProcessEnvironment:
        # `#!/usr/bin/env node` shebangs need node on PATH, which a
        # Dock-launched IDA doesn't have — node sits next to the tool script
        env = QProcessEnvironment.systemEnvironment()
        tool_dir = os.path.dirname(tool_path)
        path = env.value("PATH", "")
        if tool_dir not in path.split(":"):
            env.insert("PATH", f"{tool_dir}:{path}" if path else tool_dir)
        return env

    # ------------------------------------------------------------------
    # slidev dev-server pipeline
    # ------------------------------------------------------------------
    def _run_slidev(self, md_path: str) -> None:
        slidev = find_slidev()
        if slidev is None:
            self._show_status(
                "slidev CLI not found — install with: npm i -g @slidev/cli"
            )
            return

        if (
            self._slidev_proc is not None
            and self._slidev_proc.state() == QProcess.ProcessState.Running
            and self._slidev_md == md_path
        ):
            self._prepare_md(md_path)
            self._load_url(f"http://127.0.0.1:{self._slidev_port}/")
            return

        self._stop_slidev()
        prepared, _changed = self._prepare_md(md_path)

        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]

        self._show_status("slidev starting…")
        proc = QProcess(self)
        proc.setProgram(slidev)
        proc.setArguments([prepared, "--port", str(port), "--force"])
        proc.setStandardInputFile(QProcess.nullDevice())
        proc.setWorkingDirectory(os.path.dirname(md_path))
        proc.setProcessEnvironment(self._tool_env(slidev))
        proc.errorOccurred.connect(
            lambda _e: self._show_status("slidev failed to start")
        )
        proc.finished.connect(self._on_slidev_exit)
        self._slidev_proc = proc
        self._slidev_md = md_path
        self._slidev_port = port
        proc.start()

        self._poll_tries = 0
        self._poll_timer = QTimer(self)
        self._poll_timer.timeout.connect(self._poll_slidev_ready)
        self._poll_timer.start(400)

    def _poll_slidev_ready(self) -> None:
        if self._slidev_proc is None or self._slidev_port is None:
            if self._poll_timer:
                self._poll_timer.stop()
            return
        self._poll_tries += 1
        try:
            with socket.create_connection(("127.0.0.1", self._slidev_port), 0.2):
                pass
        except OSError:
            if self._poll_tries > 150:  # ~60s: give up
                self._poll_timer.stop()
                err = bytes(
                    self._slidev_proc.readAllStandardError()
                ).decode("utf-8", "replace").strip().splitlines()
                self._show_status(
                    "slidev did not come up"
                    + (f": {err[-1]}" if err else "")
                )
            return
        self._poll_timer.stop()
        self._show_status("")
        self._load_url(f"http://127.0.0.1:{self._slidev_port}/")

    def _on_slidev_exit(self, exit_code: int, _status) -> None:
        if self._poll_timer is not None:
            self._poll_timer.stop()
        proc, self._slidev_proc = self._slidev_proc, None
        if proc is not None and exit_code != 0:
            err = bytes(proc.readAllStandardError()).decode(
                "utf-8", "replace"
            ).strip().splitlines()
            out = bytes(proc.readAllStandardOutput()).decode(
                "utf-8", "replace"
            ).strip().splitlines()
            detail = (err or out)
            self._show_status(
                f"slidev exited ({exit_code})"
                + (f": {detail[-1]}" if detail else "")
            )

    def _stop_slidev(self) -> None:
        if self._poll_timer is not None:
            self._poll_timer.stop()
            self._poll_timer = None
        if self._slidev_proc is not None:
            proc, self._slidev_proc = self._slidev_proc, None
            try:
                proc.finished.disconnect(self._on_slidev_exit)
            except (RuntimeError, TypeError):
                pass
            proc.terminate()
            if not proc.waitForFinished(1500):
                proc.kill()
                proc.waitForFinished(1000)
        self._slidev_md = None
        self._slidev_port = None

    # ------------------------------------------------------------------
    # marp CLI pipeline
    # ------------------------------------------------------------------
    def _run_marp(self, md_path: str, restore_hash: str | None = None) -> None:
        """Render via a persistent `marp -w` watcher.

        The watcher is started once per deck and re-renders whenever the
        prepared md is rewritten, so a save costs one render instead of a
        node cold-start + render. It is stopped on cleanup, when another
        file is loaded, and when the deck switches to the slidev engine.
        """
        if self._marp is None:
            self._marp = find_marp()
        if self._marp is None:
            self._show_status(
                "marp CLI not found — install with: npm i -g @marp-team/marp-cli"
            )
            return

        stem = os.path.splitext(os.path.basename(md_path))[0]
        out = os.path.join(os.path.dirname(md_path), f".{stem}.ida-slides.html")
        prepared, changed = self._prepare_md(md_path)

        # start (or reuse) the watcher; `marp -w` logs "=> <out>" to stderr
        # each time it finishes a render, which is our completion signal —
        # mtime can't tell "this save's render" from an earlier one, and can
        # be seen mid-write
        fresh = self._proc is None or self._watch_key != (prepared, out)
        if fresh:
            self._stop_marp()
            proc = QProcess(self)
            proc.setProgram(self._marp)
            proc.setArguments(["-w", prepared, "-o", out, "--html"])
            # marp blocks reading stdin when it is a pipe
            proc.setStandardInputFile(QProcess.nullDevice())
            proc.setProcessEnvironment(self._tool_env(self._marp))
            proc.finished.connect(self._on_marp_exit)
            proc.errorOccurred.connect(self._on_marp_error)
            proc.readyReadStandardError.connect(self._drain_marp_stderr)
            self._proc = proc
            self._watch_key = (prepared, out)
            proc.start()

        # await the next "=> out" render-complete line before loading. A
        # reused watcher whose input didn't change never re-renders, so it
        # would never log again — load what's already there directly.
        if not fresh and not changed and os.path.isfile(out):
            self._finish_render(out, restore_hash)
        else:
            self._pending_out = out
            self._pending_restore = restore_hash
            self._arm_render_timeout()

    def _arm_render_timeout(self) -> None:
        if self._render_timeout is not None:
            self._render_timeout.stop()
            self._render_timeout.deleteLater()
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.timeout.connect(self._on_render_timeout)
        self._render_timeout = timer
        timer.start(15000)  # marp died mid-render, or the deck is enormous

    def _on_render_timeout(self) -> None:
        self._render_timeout = None
        if self._pending_out is None:
            return
        self._pending_out = None
        detail = f": {self._last_marp_err}" if self._last_marp_err else ""
        self._show_status(f"marp render failed{detail}")

    def _finish_render(self, out: str, restore_hash: str | None = None) -> None:
        """A render for `out` completed — swap it into the view."""
        if self._render_timeout is not None:
            self._render_timeout.stop()
            self._render_timeout.deleteLater()
            self._render_timeout = None
        self._pending_out = None
        self._pending_restore = None
        self._show_status("")
        if self._generated and self._generated != out:
            self._remove_generated()
        self._generated = out
        # set the restore hash immediately before starting THIS navigation,
        # so no earlier in-flight load's didFinishNavigation can consume it
        self._pending_hash = restore_hash
        self._load_html(out)

    def _stop_marp(self) -> None:
        if self._render_timeout is not None:
            self._render_timeout.stop()
            self._render_timeout.deleteLater()
            self._render_timeout = None
        self._pending_out = None
        self._pending_restore = None
        self._watch_key = None
        if self._proc is not None:
            # disconnect before killing: the dead watcher's queued signals
            # must not fire into handlers that touch the replacement (same
            # defense as _stop_slidev)
            proc, self._proc = self._proc, None
            try:
                proc.finished.disconnect()
                proc.errorOccurred.disconnect()
                proc.readyReadStandardError.disconnect()
            except (RuntimeError, TypeError):
                pass
            proc.kill()
            proc.waitForFinished(1000)

    def _on_marp_error(self, _error) -> None:
        self._proc = None
        self._watch_key = None
        self._show_status("marp CLI failed to start")

    def _on_marp_exit(self, code: int, _status) -> None:
        # the watcher should outlive every save; if it dies, say so and
        # let the next save/reload start a fresh one
        self._drain_marp_stderr()
        self._proc = None
        self._watch_key = None
        detail = f": {self._last_marp_err}" if self._last_marp_err else ""
        self._show_status(f"marp watcher exited (code {code}){detail}")

    def _drain_marp_stderr(self) -> None:
        if self._proc is None:
            return
        data = bytes(self._proc.readAllStandardError()).decode(
            "utf-8", "replace"
        )
        for line in data.splitlines():
            line = line.strip()
            if not line:
                continue
            logger.debug("marp: %s", line)
            # marp logs "[  INFO ] <in> => <out>" after each render; the
            # arrow line is our render-complete signal. _finish_render
            # clears _pending_out, so a second arrow line in the same
            # buffer won't re-trigger.
            if "=>" in line and self._pending_out is not None:
                self._finish_render(self._pending_out, self._pending_restore)
            elif "[ ERROR ]" in line or "error" in line.lower():
                self._last_marp_err = line

    def _remove_generated(self) -> None:
        for attr in ("_generated", "_generated_md"):
            path = getattr(self, attr)
            if path:
                try:
                    os.remove(path)
                except OSError:
                    pass
                setattr(self, attr, None)

    # ------------------------------------------------------------------
    # WKWebView loading
    # ------------------------------------------------------------------
    def _load_url(self, spec: str) -> None:
        if self._web is None:
            return
        try:
            import AppKit

            url = AppKit.NSURL.URLWithString_(spec)
            req = AppKit.NSURLRequest.requestWithURL_(url)
            self._web.loadRequest_(req)
        except Exception:
            logger.exception("loadRequest failed for %s", spec)

    def _load_html(self, html_path: str) -> None:
        if self._web is None:
            return
        try:
            import AppKit

            url = AppKit.NSURL.fileURLWithPath_(os.path.abspath(html_path))
            root = AppKit.NSURL.fileURLWithPath_(
                os.path.dirname(os.path.abspath(html_path))
            )
            self._web.loadFileURL_allowingReadAccessToURL_(url, root)
        except Exception:
            logger.exception("loadFileURL failed")

    def do_jump(self, name: str, line: int | None) -> None:
        """Navigate IDA without losing keyboard control of the deck.
        jump_to itself no longer leaves focus on the IDA view, but the
        WKWebView is a native NSView that Qt does not track as a focus
        child, so its first-responder status still needs a nudge."""
        _safe_jump(name, line)
        self._restore_focus()

    def _restore_focus(self) -> None:
        import ida_kernwin

        try:
            twidget = ida_kernwin.find_widget(self._form_caption)
            if twidget is not None:
                ida_kernwin.activate_widget(twidget, True)
            if self._web is not None:
                win = self._web.window()
                if win is not None:
                    win.makeFirstResponder_(self._web)
        except Exception:
            logger.exception("focus restore failed")

    def deliver_preview(
        self, name: str, line: int | None, req_id: str, key: str
    ) -> None:
        """Answer a hover-preview request from the page's JS."""
        if self._web is None:
            return
        try:
            import deck_preprocess

            text = deck_preprocess.preview_text(name, line)
        except Exception:
            logger.exception("preview failed for %s", name)
            text = ""
        js = (
            f"window.__idaSlidesPreview({json.dumps(req_id)}, "
            f"{json.dumps(key)}, {json.dumps(text)})"
        )
        try:
            self._web.evaluateJavaScript_completionHandler_(js, None)
        except Exception:
            logger.exception("preview delivery failed")

    def on_load_finished(self) -> None:
        """Called (via QTimer) after didFinishNavigation."""
        if self._web is None:
            return
        try:
            js = ""
            if self._pending_hash:
                import marp_markdown

                js = marp_markdown.bespoke_restore_js(self._pending_hash)
                self._pending_hash = None
            # Bespoke measures the viewport once at load; when the load
            # lands mid-layout the slide stays scaled to a stale size
            # (white deck + black letterbox corner) until something
            # resizes the pane. Force a re-measure every load.
            js += "window.dispatchEvent(new Event('resize'));"
            self._web.evaluateJavaScript_completionHandler_(js, None)
        except Exception:
            logger.exception("post-load fixup failed")
