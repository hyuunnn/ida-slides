import logging
import os

import ida_idaapi
import ida_kernwin

from presenter_form import FILE_FILTER, SlidesForm

logger = logging.getLogger(__name__)

ACTION_NAME = "ida_slides:open"
ACTION_LABEL = "ida-slides: Open Slides…"
ACTION_TOOLTIP = "Open a Marp or Slidev deck in a dockable IDA tab"
ACTION_SHORTCUT = "Ctrl+Shift+M"
MENU_PATH = "View/Open subviews/"


def _ask_and_open() -> bool:
    """Prompt for a deck and open it, warning the user on every failure.
    Shared by the menu action and the plugin's run() so both surface errors
    the same way instead of drifting apart."""
    path = ida_kernwin.ask_file(False, FILE_FILTER, "Open slide deck")
    if not path:
        return False
    if not os.path.isfile(path):
        ida_kernwin.warning(f"ida-slides: file not found:\n{path}")
        return False
    try:
        SlidesForm.show_for_file(path)
    except Exception:
        logger.exception("ida-slides: failed to open %s", path)
        ida_kernwin.warning(
            "ida-slides: failed to open the deck. See Output window for details."
        )
        return False
    return True


class _OpenSlidesHandler(ida_kernwin.action_handler_t):
    def activate(self, ctx) -> int:
        return 1 if _ask_and_open() else 0

    def update(self, ctx) -> int:
        return ida_kernwin.AST_ENABLE_ALWAYS


class ida_slides_plugmod_t(ida_idaapi.plugmod_t):
    def __init__(self):
        super().__init__()
        self._action_registered = False
        self._menu_attached = False
        self._copy_ref_registered = False
        self._register()
        self._register_copy_ref()

    def _register_copy_ref(self):
        try:
            import copy_ref

            copy_ref.register()
            self._copy_ref_registered = True
        except Exception:
            logger.exception("failed to register Copy @reference action")

    def _register(self):
        desc = ida_kernwin.action_desc_t(
            ACTION_NAME,
            ACTION_LABEL,
            _OpenSlidesHandler(),
            ACTION_SHORTCUT,
            ACTION_TOOLTIP,
            -1,
        )
        if not ida_kernwin.register_action(desc):
            logger.warning("failed to register action %s", ACTION_NAME)
            return
        self._action_registered = True

        if ida_kernwin.attach_action_to_menu(
            MENU_PATH, ACTION_NAME, ida_kernwin.SETMENU_APP
        ):
            self._menu_attached = True
        else:
            logger.warning("failed to attach %s to %s", ACTION_NAME, MENU_PATH)

    def run(self, arg):
        # Triggered by "Run plugin" — same path as the menu action.
        _ask_and_open()

    def term(self):
        try:
            SlidesForm.close_singleton()
        except Exception:
            logger.exception("ida-slides: error closing form during term")
        if self._copy_ref_registered:
            try:
                import copy_ref

                copy_ref.unregister()
            except Exception:
                logger.exception("failed to unregister Copy @reference action")
            self._copy_ref_registered = False
        if self._menu_attached:
            ida_kernwin.detach_action_from_menu(MENU_PATH, ACTION_NAME)
            self._menu_attached = False
        if self._action_registered:
            ida_kernwin.unregister_action(ACTION_NAME)
            self._action_registered = False


class ida_slides_plugin_t(ida_idaapi.plugin_t):
    flags = ida_idaapi.PLUGIN_MULTI | ida_idaapi.PLUGIN_FIX
    comment = "Open Marp or Slidev slide decks in a dockable IDA tab."
    help = "Edit → Plugins → ida-slides, or Ctrl+Shift+M."
    wanted_name = "ida-slides"
    wanted_hotkey = ""

    def init(self):
        return ida_slides_plugmod_t()
