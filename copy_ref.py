"""Right-click → "Copy @reference": put an ida-slides deck token for the
current location on the clipboard.

- Pseudocode view: `@func_name:LINE` (line omitted on the prototype line)
- Disassembly/Hex view: `@name` when the address has a name, else `@0xADDR`
"""

import logging

import ida_idaapi
import ida_kernwin
import ida_name

logger = logging.getLogger(__name__)

ACTION_NAME = "ida_slides:copy_ref"
ACTION_LABEL = "Copy @reference"
ACTION_TOOLTIP = "Copy an ida-slides deck token (@name / @name:line) for this location"

_WIDGET_TYPES = (
    ida_kernwin.BWN_DISASM,
    ida_kernwin.BWN_PSEUDOCODE,
    ida_kernwin.BWN_HEXVIEW,
)


def build_reference(widget, cur_ea: int) -> str | None:
    """Compute the @token for a context-menu click in `widget` at `cur_ea`."""
    if ida_kernwin.get_widget_type(widget) == ida_kernwin.BWN_PSEUDOCODE:
        try:
            import ida_hexrays

            vu = ida_hexrays.get_widget_vdui(widget)
            if vu is not None and vu.cfunc is not None:
                name = ida_name.get_name(vu.cfunc.entry_ea)
                if name:
                    lnnum = vu.cpos.lnnum
                    if lnnum > 0:
                        return f"@{name}:{lnnum + 1}"
                    return f"@{name}"
        except Exception:
            logger.exception("pseudocode reference failed")
        return None

    if cur_ea == ida_idaapi.BADADDR:
        return None
    name = ida_name.get_name(cur_ea)
    if name:
        return f"@{name}"
    return f"@0x{cur_ea:X}"


def _copy_to_clipboard(text: str) -> bool:
    try:
        from PySide6.QtGui import QGuiApplication

        QGuiApplication.clipboard().setText(text)
        return True
    except Exception:
        logger.exception("clipboard copy failed")
        return False


class _CopyRefHandler(ida_kernwin.action_handler_t):
    def activate(self, ctx) -> int:
        ref = build_reference(ctx.widget, ctx.cur_ea)
        if not ref:
            ida_kernwin.msg("ida-slides: nothing to reference here\n")
            return 0
        if _copy_to_clipboard(ref):
            ida_kernwin.msg(f"ida-slides: copied {ref}\n")
            return 1
        return 0

    def update(self, ctx) -> int:
        if ctx.widget_type in _WIDGET_TYPES:
            return ida_kernwin.AST_ENABLE_FOR_WIDGET
        return ida_kernwin.AST_DISABLE_FOR_WIDGET


class _PopupHook(ida_kernwin.UI_Hooks):
    def finish_populating_widget_popup(self, widget, popup_handle, ctx=None):
        if ida_kernwin.get_widget_type(widget) in _WIDGET_TYPES:
            ida_kernwin.attach_action_to_popup(
                widget, popup_handle, ACTION_NAME, None
            )


_hook: _PopupHook | None = None


def register() -> None:
    global _hook
    desc = ida_kernwin.action_desc_t(
        ACTION_NAME,
        ACTION_LABEL,
        _CopyRefHandler(),
        None,
        ACTION_TOOLTIP,
        -1,
    )
    if not ida_kernwin.register_action(desc):
        # already registered (e.g. plugin reload) — re-register cleanly
        ida_kernwin.unregister_action(ACTION_NAME)
        ida_kernwin.register_action(desc)
    _hook = _PopupHook()
    _hook.hook()


def unregister() -> None:
    global _hook
    if _hook is not None:
        _hook.unhook()
        _hook = None
    ida_kernwin.unregister_action(ACTION_NAME)
