"""Slide renderer widgets.

Two implementations with the same duck-typed surface (`load`, `reload`):

- SlideView: QTextBrowser-based, renders .md decks one slide at a time.
  Works in every IDA install (no QtWebEngine needed).
- WebSlideView: QWebEngineView-based, renders marp-cli HTML output with
  full theme fidelity. Only constructible when PySide6 QtWebEngine is
  importable (`pip install PySide6-Addons`).

Both make `@name` tokens clickable; clicks jump the IDA view.
"""

import logging
import os

import ida_kernwin
from PySide6.QtCore import QEvent, Qt, QUrl
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QTextBrowser,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

import ida_links
import marp_markdown

logger = logging.getLogger(__name__)

_NAV_KEYS_NEXT = (Qt.Key.Key_Right, Qt.Key.Key_PageDown, Qt.Key.Key_Space)
_NAV_KEYS_PREV = (Qt.Key.Key_Left, Qt.Key.Key_PageUp)


class SlideView(QWidget):
    """Markdown deck viewer: one slide per page, prev/next navigation."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._path: str | None = None
        self._slides: list[str] = []
        self._index = 0

        self._browser = QTextBrowser(self)
        self._browser.setOpenLinks(False)
        self._browser.setOpenExternalLinks(False)
        self._browser.anchorClicked.connect(self._on_anchor_clicked)
        self._browser.highlighted.connect(self._on_anchor_hovered)
        self._browser.installEventFilter(self)

        self._prev_btn = QToolButton(self)
        self._prev_btn.setText("◀")
        self._prev_btn.setAutoRepeat(True)
        self._prev_btn.clicked.connect(self.prev_slide)

        self._next_btn = QToolButton(self)
        self._next_btn.setText("▶")
        self._next_btn.setAutoRepeat(True)
        self._next_btn.clicked.connect(self.next_slide)

        self._counter = QLabel("", self)
        self._counter.setAlignment(Qt.AlignmentFlag.AlignCenter)

        nav = QHBoxLayout()
        nav.setContentsMargins(4, 2, 4, 2)
        nav.addWidget(self._prev_btn)
        nav.addStretch(1)
        nav.addWidget(self._counter)
        nav.addStretch(1)
        nav.addWidget(self._next_btn)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._browser, 1)
        layout.addLayout(nav)

    # ------------------------------------------------------------------
    def load(self, path: str) -> None:
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError as exc:
            ida_kernwin.warning(f"ida-slides: cannot read {path}:\n{exc}")
            return

        try:
            import deck_preprocess

            text = deck_preprocess.expand_embeds(text)
        except Exception:
            logger.exception("embed preprocessing failed")

        keep_position = path == self._path
        self._path = path
        self._slides = marp_markdown.parse_deck(text)
        if keep_position:
            self._index = min(self._index, len(self._slides) - 1)
        else:
            self._index = 0

        self._browser.document().setBaseUrl(
            QUrl.fromLocalFile(os.path.dirname(os.path.abspath(path)) + os.sep)
        )
        self._render()

    def reload(self) -> None:
        if self._path:
            self.load(self._path)

    def next_slide(self) -> None:
        if self._index < len(self._slides) - 1:
            self._index += 1
            self._render()

    def prev_slide(self) -> None:
        if self._index > 0:
            self._index -= 1
            self._render()

    # ------------------------------------------------------------------
    def eventFilter(self, obj, event):
        if obj is self._browser and event.type() == QEvent.Type.KeyPress:
            key = event.key()
            if event.modifiers() == Qt.KeyboardModifier.NoModifier:
                if key in _NAV_KEYS_NEXT:
                    self.next_slide()
                    return True
                if key in _NAV_KEYS_PREV:
                    self.prev_slide()
                    return True
                if key == Qt.Key.Key_Home:
                    self._index = 0
                    self._render()
                    return True
                if key == Qt.Key.Key_End:
                    self._index = len(self._slides) - 1
                    self._render()
                    return True
        return super().eventFilter(obj, event)

    def _on_anchor_hovered(self, url: QUrl) -> None:
        import html as html_mod

        from PySide6.QtGui import QCursor
        from PySide6.QtWidgets import QToolTip

        target = ida_links.name_from_url(url) if not url.isEmpty() else None
        if target is None:
            QToolTip.hideText()
            return
        name, line = target
        try:
            import deck_preprocess

            text = deck_preprocess.preview_text(name, line)
        except Exception:
            logger.exception("preview failed for %s", name)
            return
        if text:
            QToolTip.showText(
                QCursor.pos(),
                f"<pre>{html_mod.escape(text)}</pre>",
                self._browser,
            )

    def _on_anchor_clicked(self, url: QUrl) -> None:
        target = ida_links.name_from_url(url)
        if target is not None:
            name, line = target
            # jump_to never leaves focus on the IDA view, so arrow-key
            # slide control stays here without any restore dance
            ida_links.jump_to(name, line)
            return
        if url.scheme() in ("http", "https"):
            from PySide6.QtGui import QDesktopServices

            QDesktopServices.openUrl(url)

    # ------------------------------------------------------------------
    def _theme(self) -> dict[str, str]:
        pal = self._browser.palette()
        base = pal.color(QPalette.ColorRole.Base)
        text = pal.color(QPalette.ColorRole.Text)
        dark = base.value() < 128
        return {
            "bg": base.name(),
            "fg": text.name(),
            "muted": "#9aa0a6" if dark else "#5f6368",
            "accent": "#4ea1ff" if dark else "#1a63c9",
            "code_bg": "#2b2b2b" if dark else "#f1f3f4",
            "link_bg": "#2a3b52" if dark else "#dbe9ff",
            "border": "#444444" if dark else "#dddddd",
        }

    def _stylesheet(self, t: dict[str, str]) -> str:
        return (
            f"body {{ color: {t['fg']}; background-color: {t['bg']};"
            f"  font-size: 15px; margin: 24px; }}\n"
            f"h1 {{ color: {t['accent']}; font-size: 26px; }}\n"
            f"h2 {{ color: {t['accent']}; font-size: 21px; }}\n"
            f"h3 {{ font-size: 18px; }}\n"
            f"pre {{ background-color: {t['code_bg']}; padding: 8px;"
            f"  font-family: monospace; }}\n"
            f"code {{ background-color: {t['code_bg']};"
            f"  font-family: monospace; }}\n"
            f"a {{ color: {t['accent']}; }}\n"
            f"blockquote {{ color: {t['muted']}; }}\n"
            f"th, td {{ border: 1px solid {t['border']}; padding: 3px 8px; }}\n"
        )

    def _render(self) -> None:
        if not self._slides:
            self._browser.setHtml("")
            self._counter.setText("")
            return

        theme = self._theme()
        try:
            html = marp_markdown.slide_to_html(self._slides[self._index])
        except RuntimeError as exc:
            self._browser.setPlainText(str(exc))
            return
        html = ida_links.linkify_html(
            html,
            link_color=theme["accent"],
            link_bg=theme["link_bg"],
        )

        self._browser.document().setDefaultStyleSheet(self._stylesheet(theme))
        self._browser.setHtml(f"<html><body>{html}</body></html>")

        total = len(self._slides)
        self._counter.setText(f"{self._index + 1} / {total}")
        self._prev_btn.setEnabled(self._index > 0)
        self._next_btn.setEnabled(self._index < total - 1)


def create_web_slide_view(parent: QWidget | None = None) -> QWidget:
    """Build a WebSlideView; raises ImportError when QtWebEngine is absent."""
    from PySide6.QtWebEngineCore import QWebEnginePage
    from PySide6.QtWebEngineWidgets import QWebEngineView

    class _IdaNavPage(QWebEnginePage):
        def acceptNavigationRequest(self, url: QUrl, nav_type, is_main_frame):
            target = ida_links.name_from_url(url)
            if target is not None:
                name, line = target
                ida_links.jump_to(name, line)
                return False
            return super().acceptNavigationRequest(url, nav_type, is_main_frame)

    class WebSlideView(QWidget):
        def __init__(self, parent: QWidget | None = None):
            super().__init__(parent)
            self._path: str | None = None
            self._pending_hash: str | None = None

            self._web = QWebEngineView(self)
            self._web.setPage(_IdaNavPage(self._web))
            self._web.loadFinished.connect(self._on_load_finished)

            layout = QVBoxLayout(self)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.addWidget(self._web)

        def load(self, path: str) -> None:
            self._path = path
            self._web.setUrl(QUrl.fromLocalFile(path))

        def reload(self) -> None:
            """Reload preserving the current slide (Bespoke.js URL hash)."""
            if self._path is None:
                return
            self._web.page().runJavaScript(
                "window.location.hash", self._reload_with_hash
            )

        def _reload_with_hash(self, hash_value) -> None:
            if self._path is None:
                return
            self._pending_hash = (
                hash_value if isinstance(hash_value, str) and hash_value else None
            )
            target = QUrl.fromLocalFile(self._path)
            if self._web.url().matches(target, QUrl.UrlFormattingOption.RemoveFragment):
                self._web.reload()
            else:
                self._web.setUrl(target)

        def _on_load_finished(self, ok: bool) -> None:
            if not ok:
                return
            page = self._web.page()
            if self._pending_hash:
                # Bespoke.js reads the hash on hashchange: flip then restore.
                page.runJavaScript(
                    f"window.location.hash = '#/0';"
                    f"window.location.hash = {self._pending_hash!r};"
                )
                self._pending_hash = None
            page.runJavaScript(ida_links.LINKIFY_JS)

    return WebSlideView(parent)
