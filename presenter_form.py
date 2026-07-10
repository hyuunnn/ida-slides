import logging
import os

import ida_kernwin
from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QAction, QDesktopServices
from PySide6.QtWidgets import QLabel, QToolBar, QVBoxLayout, QWidget

import renderers
import webkit_view
from file_watcher import DebouncedFileWatcher

logger = logging.getLogger(__name__)

_FORM_CAPTION = "Marp Presenter"

FILE_FILTER = "*.md;*.markdown;*.html"

_MD_EXTS = (".md", ".markdown")


class MarpPresenterForm(ida_kernwin.PluginForm):
    """Dockable IDA tab that renders a Marp deck.

    .md files render in a built-in QTextBrowser slide viewer (works
    everywhere); .html files (marp-cli output) render in QtWebEngine when
    available. Both make @name tokens clickable to jump the IDA view.

    Class-level singleton: a second `show_for_file()` swaps the file in the
    existing tab rather than spawning a duplicate.
    """

    _instance: "MarpPresenterForm | None" = None

    def __init__(self):
        ida_kernwin.PluginForm.__init__(self)
        self._path: str | None = None
        self._watcher: DebouncedFileWatcher | None = None
        self._renderer: QWidget | None = None
        self._renderer_kind: str | None = None
        self._layout: QVBoxLayout | None = None
        self._status: QLabel | None = None

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
        if self._renderer is not None and hasattr(self._renderer, "cleanup"):
            self._renderer.cleanup()
        self._renderer = None
        self._renderer_kind = None
        self._layout = None
        type(self)._instance = None

    # ------------------------------------------------------------------
    # Renderer management
    # ------------------------------------------------------------------
    def _ensure_renderer(self, kind: str) -> bool:
        if self._renderer is not None and self._renderer_kind == kind:
            return True
        if self._layout is None:
            return False

        if kind == "webkit":
            new_renderer = webkit_view.MarpWebKitView()
        elif kind == "web":
            try:
                new_renderer = renderers.create_web_slide_view()
            except ImportError:
                ida_kernwin.warning(
                    "ida-slides: neither WKWebView (macOS + pyobjc) nor "
                    "QtWebEngine is available, so .html decks cannot be "
                    "rendered.\n\n"
                    "Open the Markdown (.md) deck instead — the built-in "
                    "slide viewer needs no extra packages."
                )
                return False
        else:
            new_renderer = renderers.SlideView()

        self._drop_renderer()

        self._renderer = new_renderer
        self._renderer_kind = kind
        self._layout.addWidget(new_renderer, 1)
        return True

    def _drop_renderer(self) -> None:
        if self._renderer is None:
            return
        if hasattr(self._renderer, "cleanup"):
            self._renderer.cleanup()
        if self._layout is not None:
            self._layout.removeWidget(self._renderer)
        self._renderer.deleteLater()
        self._renderer = None
        self._renderer_kind = None

    @staticmethod
    def _pick_renderer_kind(ext: str) -> str:
        if webkit_view.webkit_available():
            # native WKWebView renders real marp CLI output for both cases
            if ext in _MD_EXTS and webkit_view.find_marp() is None:
                return "md"  # no marp binary — built-in viewer
            return "webkit"
        if ext in _MD_EXTS:
            return "md"
        return "web"

    # ------------------------------------------------------------------
    # File loading
    # ------------------------------------------------------------------
    def _load(self, path: str) -> None:
        if not os.path.isfile(path):
            ida_kernwin.warning(f"ida-slides: file not found:\n{path}")
            return

        ext = os.path.splitext(path)[1].lower()
        kind = self._pick_renderer_kind(ext)
        if not self._ensure_renderer(kind):
            return

        self._path = path
        if self._watcher is not None:
            self._watcher.watch(path)
        self._renderer.load(path)
        if self._status is not None:
            if kind == "webkit":
                label = f"{self._renderer.engine_label}/WebKit"
            elif kind == "web":
                label = "Marp/WebEngine"
            else:
                label = "basic"
            self._status.setText(f"{os.path.basename(path)}  [{label}]")

    # ------------------------------------------------------------------
    # Toolbar handlers
    # ------------------------------------------------------------------
    def _on_open_clicked(self) -> None:
        path = ida_kernwin.ask_file(False, FILE_FILTER, "Open Marp deck")
        if path and os.path.isfile(path):
            self._load(path)

    def _on_reload_clicked(self) -> None:
        if self._renderer is not None:
            self._renderer.reload()

    def _on_open_browser_clicked(self) -> None:
        if self._path:
            QDesktopServices.openUrl(QUrl.fromLocalFile(self._path))

    def _on_file_changed(self, path: str) -> None:
        if path != self._path or self._renderer is None:
            return
        # slidev's HMR handles saves itself; other renderers re-render
        handler = getattr(self._renderer, "on_source_changed", None)
        if handler is not None:
            handler()
        else:
            self._renderer.reload()
