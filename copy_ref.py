"""Right-click → "Copy @reference": put an ida-slides deck token for the
current location on the clipboard.

- Pseudocode view: `@func_name:LINE` (line omitted on the prototype line)
- Disassembly/Hex view: `@name` when the address has a name, else `@0xADDR`
- Names the token grammar can't re-parse (ObjC selectors, demangled C++)
  also fall back to `@0xADDR` so the copied token always works
"""

import logging
import re

import ida_idaapi
import ida_kernwin
import ida_name

import ida_links

logger = logging.getLogger(__name__)

# a copied token is only useful if the deck-side regex can re-parse it;
# names that don't round-trip (ObjC `-[Class sel:]`, demangled C++ with
# spaces/parens, …) fall back to the raw address form
_NAME_OK = re.compile(rf"(?:{ida_links._NAME_PATTERN})\Z")


def _token_name(ea: int) -> str:
    name = ida_name.get_name(ea)
    # trailing-dot names ARE valid IDA names and re-parse fine, but the
    # injected linkifier trims trailing dots as sentence punctuation, so a
    # rendered "@test." link would resolve "test" and go nowhere — the
    # raw-address fallback keeps the copied token's always-works contract
    if name and _NAME_OK.match(name) and not name.endswith("."):
        return name
    return f"0x{ea:X}"

ACTION_NAME = "ida_slides:copy_ref"
ACTION_LABEL = "Copy @reference"
ACTION_TOOLTIP = "Copy an ida-slides deck token (@name / @name:line) for this location"

_WIDGET_TYPES = (
    ida_kernwin.BWN_DISASM,
    ida_kernwin.BWN_PSEUDOCODE,
    ida_kernwin.BWN_HEXVIEW,
)


def _pseudocode_selection_lines(widget) -> tuple[int, int] | None:
    """1-indexed (lo, hi) pseudocode line range of the current selection,
    or None if there is no multi-line selection."""
    try:
        p1 = ida_kernwin.twinpos_t()
        p2 = ida_kernwin.twinpos_t()
        if not ida_kernwin.read_selection(widget, p1, p2):
            return None
        n1 = ida_kernwin.place_t_as_simpleline_place_t(p1.place(widget)).n
        n2 = ida_kernwin.place_t_as_simpleline_place_t(p2.place(widget)).n
        (lo, _lo_x), (hi, hi_x) = sorted(((n1, p1.x), (n2, p2.x)))
        if hi > lo and hi_x == 0:
            hi -= 1  # drag released at column 0 — that line isn't selected
        if hi <= lo:
            return None
        return lo + 1, hi + 1
    except Exception:
        logger.exception("reading pseudocode selection failed")
        return None


def build_reference(widget, cur_ea: int) -> str | None:
    """Compute the @token for a context-menu action in `widget` at `cur_ea`.

    In pseudocode a multi-line selection becomes an embed token
    `@name[lo:hi]`; a single line becomes `@name:line`.
    """
    if ida_kernwin.get_widget_type(widget) == ida_kernwin.BWN_PSEUDOCODE:
        try:
            import ida_hexrays

            vu = ida_hexrays.get_widget_vdui(widget)
            if vu is not None and vu.cfunc is not None:
                name = _token_name(vu.cfunc.entry_ea)
                span = _pseudocode_selection_lines(widget)
                if span is not None:
                    return f"@{name}[{span[0]}:{span[1]}]"
                lnnum = vu.cpos.lnnum
                if lnnum > 0:
                    return f"@{name}:{lnnum + 1}"
                return f"@{name}"
        except Exception:
            logger.exception("pseudocode reference failed")
        return None

    # disassembly / hex: prefer the name at the selection start, else address
    sel_ea = _disasm_selection_start(widget)
    ea = sel_ea if sel_ea != ida_idaapi.BADADDR else cur_ea
    if ea == ida_idaapi.BADADDR:
        return None
    return f"@{_token_name(ea)}"


def _disasm_selection_start(widget) -> int:
    """Start EA of a disassembly/hex selection, or BADADDR if none."""
    try:
        ok, start, _end = ida_kernwin.read_range_selection(widget)
        if ok and start != ida_idaapi.BADADDR:
            return start
    except Exception:
        logger.exception("reading disasm selection failed")
    return ida_idaapi.BADADDR


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
