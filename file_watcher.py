import logging
import os

from PySide6.QtCore import QFileSystemWatcher, QObject, QTimer, Signal

logger = logging.getLogger(__name__)

_DEBOUNCE_MS = 200
_RECOVERY_DELAY_MS = 50
_RECOVERY_MAX_ATTEMPTS = 100  # ~5s; a deleted deck must not poll forever


class DebouncedFileWatcher(QObject):
    """QFileSystemWatcher wrapper with debounced re-emission.

    Editors on macOS (VS Code, vim with backup, marp-cli's atomic rename) replace
    the file via rename(), so the inode changes and QFileSystemWatcher silently
    drops the path. We re-arm `addPath` on every change to survive that pattern.
    """

    changed = Signal(str)

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._watcher = QFileSystemWatcher(self)
        self._watcher.fileChanged.connect(self._on_file_changed)
        self._path: str | None = None
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._emit)

    def watch(self, path: str) -> None:
        self.unwatch()
        self._path = path
        if not self._watcher.addPath(path):
            logger.warning("file_watcher: addPath failed for %s", path)

    def unwatch(self) -> None:
        if self._path is not None:
            self._watcher.removePath(self._path)
            self._path = None
        self._timer.stop()

    def _on_file_changed(self, path: str) -> None:
        # Ignore signals for a previously watched file: one queued before a
        # watch() swap can arrive late and must not fire a phantom changed()
        # for the new path. (Also covers the unwatched case: _path None.)
        if self._path != path:
            return
        # If the file was atomically replaced, the watcher dropped it.
        # Schedule a short follow-up that re-adds the path before debounced emit.
        self._schedule_rearm(0)
        self._timer.start(_DEBOUNCE_MS)

    def _schedule_rearm(self, attempt: int) -> None:
        # pass `self` as the timer's receiver so a pending shot is dropped
        # automatically when this QObject is destroyed
        QTimer.singleShot(_RECOVERY_DELAY_MS, self, lambda: self._rearm(attempt))

    def _rearm(self, attempt: int = 0) -> None:
        if self._path is None:
            return
        if self._path in self._watcher.files():
            return
        if not os.path.exists(self._path):
            # File temporarily missing during atomic rename — retry shortly,
            # but give up if it stays gone (deleted, not replaced).
            if attempt < _RECOVERY_MAX_ATTEMPTS:
                self._schedule_rearm(attempt + 1)
            else:
                logger.warning(
                    "file_watcher: %s stayed missing, giving up", self._path
                )
            return
        if not self._watcher.addPath(self._path):
            logger.warning("file_watcher: re-addPath failed for %s", self._path)

    def _emit(self) -> None:
        if self._path is not None:
            self.changed.emit(self._path)
