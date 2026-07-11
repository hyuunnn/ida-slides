import logging
import os

import ida_kernwin
from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QAction, QDesktopServices
from PySide6.QtWidgets import QLabel, QToolBar, QVBoxLayout, QWidget

import webkit_view
from file_watcher import DebouncedFileWatcher

logger = logging.getLogger(__name__)

_FORM_CAPTION = "ida-slides"

FILE_FILTER = "*.md;*.markdown;*.html"

_MD_EXTS = (".md", ".markdown")


class SlidesForm(ida_kernwin.PluginForm):
    """Dockable IDA tab that renders a Marp/Slidev deck.

    Rendering is native WKWebView only (macOS + PyObjC): .md decks go
    through the marp CLI or a slidev dev server, .html files (marp-cli
    output) load directly. There is no fallback viewer — without WKWebView
    or the engine's CLI the deck simply doesn't render.

    Class-level singleton: a second `show_for_file()` swaps the file in the
    existing tab rather than spawning a duplicate.
    """

    _instance: "SlidesForm | None" = None

    def __init__(self):
        ida_kernwin.PluginForm.__init__(self)
        self._path: str | None = None
        self._watcher: DebouncedFileWatcher | None = None
        self._renderer: QWidget | None = None
        self._layout: QVBoxLayout | None = None
        self._status: QLabel | None = None
        self._warn: QLabel | None = None
        self._status_base = ""
        self._last_lint: list[tuple[int, str]] | None = None

    # ------------------------------------------------------------------
    # Singleton entry points
    # ------------------------------------------------------------------
    @classmethod
    def show_for_file(cls, path: str) -> None:
        path = os.path.abspath(path)
        if cls._instance is None:
            # a stale widget with our caption (e.g. restored from a saved
            # desktop layout) makes Show() fail silently — close it first
            stale = ida_kernwin.find_widget(_FORM_CAPTION)
            if stale is not None:
                ida_kernwin.close_widget(stale, ida_kernwin.WCLS_DONT_SAVE_SIZE)
            cls._instance = cls()
            cls._instance._initial_path = path  # OnCreate picks it up
            cls._instance.Show(
                _FORM_CAPTION,
                options=(
                    ida_kernwin.PluginForm.WOPN_DP_TAB
                    | ida_kernwin.PluginForm.WOPN_RESTORE
                ),
            )
            # Dock the deck to the right so disassembly stays on the left.
            ida_kernwin.set_dock_pos(_FORM_CAPTION, None, ida_kernwin.DP_RIGHT)
        else:
            cls._instance._load(path)
            twidget = ida_kernwin.find_widget(_FORM_CAPTION)
            if twidget is not None:
                ida_kernwin.activate_widget(twidget, True)

    @classmethod
    def close_singleton(cls) -> None:
        if cls._instance is not None:
            try:
                cls._instance.Close(ida_kernwin.PluginForm.WCLS_SAVE)
            except Exception:
                logger.exception("close_singleton: Close failed")
            cls._instance = None

    # ------------------------------------------------------------------
    # PluginForm hooks
    # ------------------------------------------------------------------
    def OnCreate(self, form):
        # FormToPySideWidget needs QtGui in __main__ and dies with a swallowed
        # AttributeError otherwise (IDA 9.3); FormToPyQtWidget goes through
        # shiboken6.wrapInstance and works in any context.
        parent: QWidget = self.FormToPyQtWidget(form)

        toolbar = QToolBar(parent)
        toolbar.setMovable(False)

        open_act = QAction("Open…", parent)
        open_act.triggered.connect(self._on_open_clicked)
        toolbar.addAction(open_act)

        reload_act = QAction("Reload", parent)
        reload_act.setShortcut("R")
        # only fire while focus is inside this form, not all of IDA
        reload_act.setShortcutContext(
            Qt.ShortcutContext.WidgetWithChildrenShortcut
        )
        reload_act.triggered.connect(self._on_reload_clicked)
        toolbar.addAction(reload_act)
        parent.addAction(reload_act)

        browser_act = QAction("Open in Browser", parent)
        browser_act.triggered.connect(self._on_open_browser_clicked)
        toolbar.addAction(browser_act)

        toolbar.addSeparator()
        self._status = QLabel("", parent)
        toolbar.addWidget(self._status)
        # separate widget so the unresolved-refs tooltip only appears over
        # the warning text itself, not the whole status area
        self._warn = QLabel("", parent)
        toolbar.addWidget(self._warn)

        self._watcher = DebouncedFileWatcher(parent)
        self._watcher.changed.connect(self._on_file_changed)

        self._layout = QVBoxLayout(parent)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(0)
        self._layout.addWidget(toolbar)

        initial = getattr(self, "_initial_path", None)
        if initial:
            self._load(initial)

    def OnClose(self, form):
        if self._watcher is not None:
            self._watcher.unwatch()
            self._watcher = None
        if self._renderer is not None:
            self._renderer.cleanup()
        self._renderer = None
        self._layout = None
        type(self)._instance = None

    # ------------------------------------------------------------------
    # Renderer management
    # ------------------------------------------------------------------
    def _ensure_renderer(self) -> bool:
        if self._renderer is not None:
            if not getattr(self._renderer, "attach_failed", False):
                return True
            # the WKWebView never attached (broken PyObjC install etc.) —
            # rebuild instead of keeping the dead shell for the form's
            # lifetime; a fresh attach may succeed
            self._renderer.cleanup()
            if self._layout is not None:
                self._layout.removeWidget(self._renderer)
            self._renderer.deleteLater()
            self._renderer = None
        if self._layout is None:
            return False
        if not webkit_view.webkit_available():
            ida_kernwin.warning(
                "ida-slides: rendering requires macOS with PyObjC "
                "(native WKWebView).\n\n"
                "Install with: pip install --user pyobjc-framework-WebKit"
            )
            return False

        new_renderer = webkit_view.DeckWebKitView()
        new_renderer._form_caption = _FORM_CAPTION
        self._renderer = new_renderer
        self._layout.addWidget(new_renderer, 1)
        return True

    # ------------------------------------------------------------------
    # File loading
    # ------------------------------------------------------------------
    def _load(self, path: str) -> None:
        if not os.path.isfile(path):
            ida_kernwin.warning(f"ida-slides: file not found:\n{path}")
            return
        if not self._ensure_renderer():
            return

        self._path = path
        if self._watcher is not None:
            self._watcher.watch(path)
        if not self._renderer.load(path):
            # the renderer displayed why; don't stamp a status line with a
            # stale engine label or clear the previous deck's lint warning
            # for a deck that was never parsed
            return
        label = f"{self._renderer.engine_label}/WebKit"
        self._status_base = f"{os.path.basename(path)}  [{label}]"
        self._refresh_status()

    # ------------------------------------------------------------------
    # Toolbar handlers
    # ------------------------------------------------------------------
    def _on_open_clicked(self) -> None:
        path = ida_kernwin.ask_file(False, FILE_FILTER, "Open slide deck")
        if path and os.path.isfile(path):
            self._load(path)

    def _on_reload_clicked(self) -> None:
        if self._renderer is not None:
            self._renderer.reload()
            self._refresh_status()

    def _on_open_browser_clicked(self) -> None:
        if self._path:
            QDesktopServices.openUrl(QUrl.fromLocalFile(self._path))

    # ------------------------------------------------------------------
    # Reference lint
    # ------------------------------------------------------------------
    def _refresh_status(self) -> None:
        """Show the deck label plus a warning when @references are broken."""
        issues = self._lint_refs()
        if self._status is not None:
            self._status.setText(self._status_base)
        if self._warn is None:
            return
        if issues:
            self._warn.setText(f"  ⚠ {len(issues)} unresolved @ref(s)")
            self._warn.setToolTip(
                "\n".join(f"{tok} — slide {n}" for n, tok in issues)
            )
        else:
            self._warn.setText("")
            self._warn.setToolTip("")

    def _lint_refs(self) -> list[tuple[int, str]]:
        """Check every @reference in the deck against the open IDB."""
        if (
            not self._path
            or os.path.splitext(self._path)[1].lower() not in _MD_EXTS
        ):
            return []
        try:
            import deck_preprocess

            with open(self._path, encoding="utf-8", errors="replace") as fh:
                issues = deck_preprocess.unresolved_refs(fh.read())
        except Exception:
            logger.exception("reference lint failed for %s", self._path)
            return []
        if issues and issues != self._last_lint:
            listing = ", ".join(f"{tok} (slide {n})" for n, tok in issues)
            ida_kernwin.msg(
                f"ida-slides: {len(issues)} unresolved @reference(s): "
                f"{listing}\n"
            )
        self._last_lint = issues
        return issues

    def _on_file_changed(self, path: str) -> None:
        # the watcher only ever emits its currently watched path (stale
        # signals are dropped inside DebouncedFileWatcher)
        if self._renderer is None:
            return
        # slidev's HMR handles saves itself; the marp path re-renders
        self._renderer.on_source_changed()
        self._refresh_status()
