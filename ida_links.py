"""Turn `@name` / `@0xADDR` tokens into clickable links that jump IDA views.

The token grammar lives here; the actual linkification happens in
`deck_view.USER_JS` (injected into the rendered DOM on both platforms),
which gets the grammar via `JS_TOKEN_RE`.
"""

import logging
import re

logger = logging.getLogger(__name__)

# @sub_401000, @main, @_Z3foov, @.init_proc, @0x401000
# an optional :N suffix targets pseudocode line N (@sub_401000:22);
# the lookbehind keeps the @ in emails (user@host) from starting a token
_NAME_PATTERN = r"0x[0-9A-Fa-f]+|[A-Za-z_?$.][\w?$@.]*"
TOKEN_RE = re.compile(rf"(?<![A-Za-z0-9_@])@({_NAME_PATTERN})(?::(\d+))?")

# the same grammar as a JS regex literal — single source for the injected
# linkifier (deck_view.USER_JS); the JS side does the email-@ guard as a
# prev-char check instead of a lookbehind
JS_TOKEN_RE = rf"/@({_NAME_PATTERN})(?::(\d+))?/g"


def resolve_ea(name: str) -> int:
    import ida_idaapi
    import ida_name

    if name.lower().startswith("0x"):
        try:
            ea = int(name, 16)
        except ValueError:
            return ida_idaapi.BADADDR
        # a value beyond ea_t overflows SWIG converters downstream
        # (getseg etc.), which would blow up a whole lint pass — treat
        # out-of-range hex as unresolvable instead
        return ea if ea < ida_idaapi.BADADDR else ida_idaapi.BADADDR
    return ida_name.get_name_ea(ida_idaapi.BADADDR, name)


def is_resolvable(name: str) -> bool:
    import ida_idaapi

    return resolve_ea(name) != ida_idaapi.BADADDR


def jump_to(name: str, line: int | None = None) -> bool:
    import ida_idaapi
    import ida_kernwin

    ea = resolve_ea(name)
    if ea == ida_idaapi.BADADDR:
        ida_kernwin.msg(f"ida-slides: no such name/address: {name}\n")
        return False
    if line is None:
        return _jump_no_focus(ea)
    return _jump_to_pseudocode_line(ea, line, name)


def _jump_no_focus(ea: int) -> bool:
    """jumpto without UIJMP_ACTIVATE, so keyboard focus (and arrow-key
    slide control) stays on the presenter instead of the IDA view."""
    import ida_kernwin

    ok = ida_kernwin.jumpto(ea, -1, 0)
    w = ida_kernwin.find_widget("IDA View-A")
    if w is not None:
        # a non-activating jump repositions a buried tab but does not
        # raise it; raise explicitly while leaving focus alone
        ida_kernwin.activate_widget(w, False)
        return ok
    # no default disasm view to surface — let an activating jump open
    # one, then hand focus straight back to whoever had it
    prev = ida_kernwin.get_current_widget()
    ok = ida_kernwin.jumpto(ea)
    if prev is not None:
        ida_kernwin.activate_widget(prev, True)
    return ok


def _jump_to_pseudocode_line(ea: int, line: int, name: str) -> bool:
    """Open the decompiler view for `ea` positioned at 1-indexed `line`."""
    import ida_kernwin

    try:
        import ida_hexrays

        # open_pseudocode focuses the pseudocode view even when reusing
        # an existing one; hand focus straight back to the deck
        prev = ida_kernwin.get_current_widget()
        vu = ida_hexrays.open_pseudocode(ea, ida_hexrays.OPF_REUSE)
        if vu is None:
            raise RuntimeError("open_pseudocode failed")
        if prev is not None:
            ida_kernwin.activate_widget(prev, True)
        nlines = vu.cfunc.get_pseudocode().size()
        lnnum = min(max(line, 1), nlines) - 1
        title = ida_kernwin.get_widget_title(vu.ct)
        entry = vu.cfunc.entry_ea

        from PySide6.QtCore import QTimer

        def _position(attempt: int = 0) -> None:
            # Opening a *different* function queues Hex-Rays' own entry-point
            # jump, which can land after ours and clobber it. We re-apply and
            # verify a few times until the caret sticks (or give up quietly).
            try:
                # re-resolve the viewer on every attempt instead of reusing
                # the captured TWidget: the user can close (or Hex-Rays can
                # replace) the tab between retries, and touching the freed
                # widget can hard-crash IDA — not a catchable exception
                w = ida_kernwin.find_widget(title)
                vu2 = ida_hexrays.get_widget_vdui(w) if w is not None else None
                if vu2 is None or vu2.cfunc is None:
                    return  # view is gone — give up quietly
                if vu2.cfunc.entry_ea != entry:
                    return  # view shows another function now
                ct = vu2.ct
                # raise the pseudocode tab but keep keyboard focus on the
                # deck (take_focus=False); the retry loop below would
                # otherwise re-steal focus on every attempt
                ida_kernwin.activate_widget(ct, False)
                # simpleline_place_t's constructor is abstract in IDA 9.3's
                # bindings; clone the viewer's place and cast it. NB: the cast
                # returns a fresh proxy each call, so the object we mutate must
                # be the one we hand to jumpto — mutating a throwaway is a no-op.
                clone = ida_kernwin.get_custom_viewer_place(ct, False)[0].clone()
                sp = ida_kernwin.place_t_as_simpleline_place_t(clone)
                sp.n = lnnum
                ida_kernwin.jumpto(ct, sp, 0, 0)

                cur, _x, _y = ida_kernwin.get_custom_viewer_place(ct, False)
                landed = ida_kernwin.place_t_as_simpleline_place_t(cur).n
                if landed != lnnum and attempt < 6:
                    QTimer.singleShot(60, lambda: _position(attempt + 1))
            except Exception:
                if attempt < 6:
                    QTimer.singleShot(60, lambda: _position(attempt + 1))
                else:
                    logger.exception("pseudocode line positioning failed")

        QTimer.singleShot(60, _position)
        return True
    except Exception:
        logger.exception("pseudocode line jump failed for %s:%d", name, line)
        ida_kernwin.msg(
            f"ida-slides: cannot open pseudocode for {name}; "
            "jumping to the function instead\n"
        )
        return _jump_no_focus(ea)


