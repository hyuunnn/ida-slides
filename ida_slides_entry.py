import logging
import os

import ida_kernwin

logger = logging.getLogger(__name__)


def _disable_reason() -> str | None:
    if not ida_kernwin.is_idaq():
        return "ida-slides: not running in idaq (text/headless mode)"

    if os.environ.get("IDA_IS_INTERACTIVE") != "1":
        return "ida-slides: IDA_IS_INTERACTIVE != 1"

    kernel_version = tuple(
        int(part)
        for part in ida_kernwin.get_kernel_version().split(".")
        if part.isdigit()
    ) or (0,)
    if kernel_version < (9, 2):
        return f"ida-slides: IDA too old (need 9.2+): {ida_kernwin.get_kernel_version()}"

    try:
        import PySide6  # noqa: F401
    except ImportError as exc:
        return f"ida-slides: PySide6 not importable ({exc})"

    # WKWebView (macOS + PyObjC) and the marp/slidev CLIs are checked when
    # a deck is actually opened, so the plugin itself always loads.
    return None


_REASON = _disable_reason()

if _REASON is None:
    from ida_slides import ida_slides_plugin_t

    def PLUGIN_ENTRY():
        return ida_slides_plugin_t()

else:
    logger.warning(_REASON)

    try:
        import ida_idaapi
    except ImportError:
        import idaapi as ida_idaapi

    class _ida_slides_nop_plugin_t(ida_idaapi.plugin_t):
        flags = ida_idaapi.PLUGIN_HIDE | ida_idaapi.PLUGIN_UNL
        wanted_name = "ida-slides (disabled)"
        comment = _REASON or "ida-slides is disabled in this IDA environment"
        help = ""
        wanted_hotkey = ""

        def init(self):
            return ida_idaapi.PLUGIN_SKIP

        def run(self, arg):  # pragma: no cover - never invoked
            pass

        def term(self):  # pragma: no cover - never invoked
            pass

    def PLUGIN_ENTRY():
        return _ida_slides_nop_plugin_t()
