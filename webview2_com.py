"""Minimal ctypes-only COM layer for embedding Microsoft Edge WebView2.

Windows counterpart of the PyObjC/WKWebView glue: the system WebView2
runtime (evergreen, present on Windows 10/11) is driven directly over COM
with stdlib ctypes — no pip dependency, and no contact with IDA's bundled
Qt ABI (the same reason macOS uses the system WebKit instead of a
QtWebEngine wheel). Only the tiny official loader DLL is shipped
(win/WebView2Loader.dll, from the Microsoft.Web.WebView2 NuGet package).

Interface IIDs and vtable slot indices below are generated from the
official SDK header (WebView2.h) — they are ABI, frozen since the first
runtime release, so hardcoding them is safe. Do NOT reorder.

CRASH SAFETY (mirrors webkit_view's PyObjC rules): an exception escaping a
ctypes COM callback would corrupt the caller's HRESULT contract, so every
callback body is wrapped and never raises. All callbacks arrive on the UI
thread via the message pump; IDA work must still be deferred out of them
(QTimer.singleShot(0, ...)) by the caller — re-entrancy from inside a COM
event dispatch is not a safe place to pump IDA.
"""

import ctypes
import logging
import os
from ctypes import wintypes

logger = logging.getLogger(__name__)

HRESULT = ctypes.c_long
S_OK = 0
E_NOINTERFACE = -2147467262  # 0x80004002
E_POINTER = -2147467261     # 0x80004003

_ole32 = ctypes.WinDLL("ole32")
_ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
_ole32.CoTaskMemFree.restype = None
_ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.c_uint]
_ole32.CoInitializeEx.restype = HRESULT


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_ulong),
        ("Data2", ctypes.c_ushort),
        ("Data3", ctypes.c_ushort),
        ("Data4", ctypes.c_ubyte * 8),
    ]

    def __init__(self, text: str | None = None):
        super().__init__()
        if text:
            parts = text.strip("{}").split("-")
            self.Data1 = int(parts[0], 16)
            self.Data2 = int(parts[1], 16)
            self.Data3 = int(parts[2], 16)
            rest = bytes.fromhex(parts[3] + parts[4])
            for i, b in enumerate(rest):
                self.Data4[i] = b


def _guid_eq(a: GUID, b: GUID) -> bool:
    return bytes(a) == bytes(b)


_IID_IUNKNOWN = GUID("00000000-0000-0000-C000-000000000046")

# --- interface IIDs (from WebView2.h, MIDL_INTERFACE lines) -----------------
IID_EnvironmentCompletedHandler = "4e8a3389-c9d8-4bd2-b6b5-124fee6cc14d"
IID_ControllerCompletedHandler = "6c4819f3-c9b7-4260-8127-c9f5bde7f68c"
IID_WebMessageReceivedEventHandler = "57213f19-00e6-49fa-8e07-898ea01ecbd2"
IID_NavigationCompletedEventHandler = "d33a35bf-1c49-4f98-93ab-006e0533fe1c"
IID_ExecuteScriptCompletedHandler = "49511172-cc67-4bca-9923-137112f4c4cc"
IID_AddScriptCompletedHandler = "b99369f3-9b11-47b5-bc6f-8e7895fcea17"
IID_NewWindowRequestedEventHandler = "d4c185fe-c81c-4989-97af-2d3fa7ab5651"

EventRegistrationToken = ctypes.c_longlong

# calling prototypes for runtime-owned COM methods, built once — put_Bounds
# runs on every dock-resize event, so per-call WINFUNCTYPE allocation is a
# hot-path cost (and needless GC churn everywhere else)
_P_ULONG = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)
_P_HR = ctypes.WINFUNCTYPE(HRESULT, ctypes.c_void_p)
_P_HR_INT = ctypes.WINFUNCTYPE(HRESULT, ctypes.c_void_p, ctypes.c_int)
_P_HR_RECT = ctypes.WINFUNCTYPE(HRESULT, ctypes.c_void_p, wintypes.RECT)
_P_HR_WSTR = ctypes.WINFUNCTYPE(HRESULT, ctypes.c_void_p, ctypes.c_wchar_p)
_P_HR_PPV = ctypes.WINFUNCTYPE(
    HRESULT, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)
)
_P_HR_HWND_PTR = ctypes.WINFUNCTYPE(
    HRESULT, ctypes.c_void_p, wintypes.HWND, ctypes.c_void_p
)
_P_HR_WSTR_PTR = ctypes.WINFUNCTYPE(
    HRESULT, ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_void_p
)
_P_HR_PTR_PTOKEN = ctypes.WINFUNCTYPE(
    HRESULT, ctypes.c_void_p, ctypes.c_void_p,
    ctypes.POINTER(EventRegistrationToken),
)

# --- COM callback objects implemented in Python ------------------------------
_QI_PROTO = ctypes.WINFUNCTYPE(
    HRESULT, ctypes.c_void_p, ctypes.POINTER(GUID),
    ctypes.POINTER(ctypes.c_void_p),
)
_ADDREF_PROTO = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)
_RELEASE_PROTO = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)

# the three Invoke shapes this module needs
INVOKE_HR_PTR = ctypes.WINFUNCTYPE(
    HRESULT, ctypes.c_void_p, HRESULT, ctypes.c_void_p
)
INVOKE_PTR_PTR = ctypes.WINFUNCTYPE(
    HRESULT, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p
)
INVOKE_HR_WSTR = ctypes.WINFUNCTYPE(
    HRESULT, ctypes.c_void_p, HRESULT, ctypes.c_wchar_p
)


class _COMObj(ctypes.Structure):
    _fields_ = [("lpVtbl", ctypes.c_void_p)]


# COM objects the runtime still references; keyed by object address so the
# `this` pointer in callbacks maps back to the Python instance
_LIVE: dict[int, "ComCallback"] = {}


class ComCallback:
    """A single-method COM object (IUnknown + Invoke), the shape of every
    WebView2 completion/event handler. `fn(a, b)` receives Invoke's two
    arguments raw; it must never raise (this class guards anyway)."""

    def __init__(self, iid: str, invoke_proto, fn):
        self._fn = fn
        self._iid = GUID(iid)
        self._ref = 1
        # bound prototypes must outlive the vtable — keep them on self
        self._cb_qi = _QI_PROTO(self._query_interface)
        self._cb_addref = _ADDREF_PROTO(self._add_ref)
        self._cb_release = _RELEASE_PROTO(self._release)
        self._cb_invoke = invoke_proto(self._invoke)
        self._vtbl = (ctypes.c_void_p * 4)(
            ctypes.cast(self._cb_qi, ctypes.c_void_p),
            ctypes.cast(self._cb_addref, ctypes.c_void_p),
            ctypes.cast(self._cb_release, ctypes.c_void_p),
            ctypes.cast(self._cb_invoke, ctypes.c_void_p),
        )
        self._obj = _COMObj(ctypes.cast(self._vtbl, ctypes.c_void_p))
        self.ptr = ctypes.addressof(self._obj)
        _LIVE[self.ptr] = self

    # NOTE: the initial refcount of 1 is the reference we hand to the API
    # call taking this handler; WebView2 releases it when done, which is
    # what finally drops the object from _LIVE.

    def dispose(self) -> None:
        """Drop the construction reference when the API call that was meant
        to take it failed synchronously — otherwise the object leaks in
        _LIVE (the runtime never saw it, so it will never Release it)."""
        self._release(None)

    def _query_interface(self, this, riid, out):
        try:
            if not out:
                return E_POINTER
            if riid and (
                _guid_eq(riid.contents, _IID_IUNKNOWN)
                or _guid_eq(riid.contents, self._iid)
            ):
                out[0] = this
                self._ref += 1
                return S_OK
            out[0] = None
            return E_NOINTERFACE
        except Exception:
            logger.exception("QueryInterface failed")
            return E_NOINTERFACE

    def _add_ref(self, _this):
        self._ref += 1
        return self._ref

    def _release(self, _this):
        self._ref -= 1
        if self._ref <= 0:
            _LIVE.pop(self.ptr, None)
            return 0
        return self._ref

    def _invoke(self, _this, a, b):
        try:
            self._fn(a, b)
        except Exception:
            logger.exception("COM callback failed")
        return S_OK


# --- calling methods on runtime-owned COM objects ----------------------------
def _com_method(ptr: int, index: int, proto):
    vtbl = ctypes.cast(
        ctypes.c_void_p(ptr),
        ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)),
    ).contents
    return proto(vtbl[index])


def _read_cotaskmem_wstr(raw: int | None) -> str | None:
    if not raw:
        return None
    try:
        return ctypes.wstring_at(raw)
    finally:
        _ole32.CoTaskMemFree(raw)


class _Unknown:
    """Thin owner of a COM interface pointer. `release()` is explicit —
    no __del__ magic that could fire at interpreter-shutdown time."""

    def __init__(self, ptr: int, add_ref: bool = False):
        self.ptr = ptr
        if add_ref:
            _com_method(ptr, 1, _P_ULONG)(ptr)

    def release(self) -> None:
        if self.ptr:
            try:
                _com_method(self.ptr, 2, _P_ULONG)(self.ptr)
            except Exception:
                logger.exception("Release failed")
            self.ptr = 0


# vtable slot indices, counted from WebView2.h (IUnknown occupies 0..2)
class WebView2Environment(_Unknown):
    _CREATE_CONTROLLER = 3

    def create_controller(self, hwnd: int, callback) -> None:
        """callback(WebView2Controller | None, hresult) — deferred, invoked
        by the runtime through the message pump."""

        def _done(hr, ptr):
            ctrl = WebView2Controller(ptr, add_ref=True) if hr == S_OK and ptr else None
            callback(ctrl, hr)

        handler = ComCallback(IID_ControllerCompletedHandler, INVOKE_HR_PTR, _done)
        hr = _com_method(self.ptr, self._CREATE_CONTROLLER, _P_HR_HWND_PTR)(
            self.ptr, hwnd, handler.ptr
        )
        if hr != S_OK:
            handler.dispose()
            callback(None, hr)


class WebView2Controller(_Unknown):
    _PUT_IS_VISIBLE = 4
    _PUT_BOUNDS = 6
    _MOVE_FOCUS = 12
    _NOTIFY_PARENT_MOVED = 23
    _CLOSE = 24
    _GET_COREWEBVIEW2 = 25

    MOVE_FOCUS_PROGRAMMATIC = 0

    def put_is_visible(self, visible: bool) -> None:
        _com_method(self.ptr, self._PUT_IS_VISIBLE, _P_HR_INT)(
            self.ptr, 1 if visible else 0
        )

    def put_bounds(self, left: int, top: int, right: int, bottom: int) -> None:
        _com_method(self.ptr, self._PUT_BOUNDS, _P_HR_RECT)(
            self.ptr, wintypes.RECT(left, top, right, bottom)
        )

    def move_focus(self, reason: int = MOVE_FOCUS_PROGRAMMATIC) -> None:
        _com_method(self.ptr, self._MOVE_FOCUS, _P_HR_INT)(self.ptr, reason)

    def notify_parent_window_position_changed(self) -> None:
        _com_method(self.ptr, self._NOTIFY_PARENT_MOVED, _P_HR)(self.ptr)

    def close(self) -> None:
        try:
            _com_method(self.ptr, self._CLOSE, _P_HR)(self.ptr)
        except Exception:
            logger.exception("controller Close failed")

    def get_core_webview2(self) -> "WebView2 | None":
        out = ctypes.c_void_p()
        hr = _com_method(self.ptr, self._GET_COREWEBVIEW2, _P_HR_PPV)(
            self.ptr, ctypes.byref(out)
        )
        if hr != S_OK or not out.value:
            return None
        return WebView2(out.value)  # out-param arrives AddRef'd for us


class WebView2(_Unknown):
    _NAVIGATE = 5
    _ADD_NAVIGATION_COMPLETED = 15
    _ADD_SCRIPT_ON_CREATED = 27
    _EXECUTE_SCRIPT = 29
    _RELOAD = 31
    _ADD_WEB_MESSAGE_RECEIVED = 34
    _ADD_NEW_WINDOW_REQUESTED = 44

    def navigate(self, uri: str) -> None:
        hr = _com_method(self.ptr, self._NAVIGATE, _P_HR_WSTR)(self.ptr, uri)
        if hr != S_OK:
            logger.error("Navigate(%s) failed: 0x%08x", uri, hr & 0xFFFFFFFF)

    def reload(self) -> None:
        _com_method(self.ptr, self._RELOAD, _P_HR)(self.ptr)

    def add_script_to_execute_on_document_created(self, js: str) -> None:
        # fire-and-forget: the completion handler only logs failures
        def _done(hr, _script_id):
            if hr != S_OK:
                logger.error(
                    "AddScriptToExecuteOnDocumentCreated failed: 0x%08x",
                    hr & 0xFFFFFFFF,
                )

        handler = ComCallback(IID_AddScriptCompletedHandler, INVOKE_HR_WSTR, _done)
        hr = _com_method(self.ptr, self._ADD_SCRIPT_ON_CREATED, _P_HR_WSTR_PTR)(
            self.ptr, js, handler.ptr
        )
        if hr != S_OK:
            handler.dispose()
            logger.error(
                "AddScriptToExecuteOnDocumentCreated call failed: 0x%08x",
                hr & 0xFFFFFFFF,
            )

    def execute_script(self, js: str, callback=None) -> None:
        """callback(result_json | None): the result is JSON-encoded by the
        runtime ('"abc"' for a string, 'null' for undefined)."""

        def _done(hr, result_json):
            if callback is not None:
                callback(result_json if hr == S_OK else None)

        handler = ComCallback(IID_ExecuteScriptCompletedHandler, INVOKE_HR_WSTR, _done)
        hr = _com_method(self.ptr, self._EXECUTE_SCRIPT, _P_HR_WSTR_PTR)(
            self.ptr, js, handler.ptr
        )
        if hr != S_OK:
            handler.dispose()
            if callback is not None:
                callback(None)

    def _add_event(self, slot: int, iid: str, fn) -> int:
        handler = ComCallback(iid, INVOKE_PTR_PTR, fn)
        token = EventRegistrationToken()
        hr = _com_method(self.ptr, slot, _P_HR_PTR_PTOKEN)(
            self.ptr, handler.ptr, ctypes.byref(token)
        )
        if hr != S_OK:
            handler.dispose()
            logger.error("add_ event (slot %d) failed: 0x%08x", slot, hr & 0xFFFFFFFF)
        return token.value

    def add_web_message_received(self, fn) -> int:
        """fn(message_json: str) — the page's postMessage payload as JSON."""

        def _event(_sender, args):
            out = ctypes.c_void_p()
            # ICoreWebView2WebMessageReceivedEventArgs::get_WebMessageAsJson
            # (slot 4: IUnknown 0..2, get_Source 3)
            hr = _com_method(args, 4, _P_HR_PPV)(args, ctypes.byref(out))
            if hr == S_OK:
                text = _read_cotaskmem_wstr(out.value)
                if text:
                    fn(text)

        return self._add_event(
            self._ADD_WEB_MESSAGE_RECEIVED, IID_WebMessageReceivedEventHandler,
            _event,
        )

    def add_navigation_completed(self, fn) -> int:
        """fn() — called after every top-level navigation finishes."""

        def _event(_sender, _args):
            fn()

        return self._add_event(
            self._ADD_NAVIGATION_COMPLETED, IID_NavigationCompletedEventHandler,
            _event,
        )

    def add_new_window_requested(self, fn) -> int:
        """fn(uri) — popup/new-window requests; always marked Handled so no
        stray top-level browser window ever opens out of the dock."""

        def _event(_sender, args):
            out = ctypes.c_void_p()
            # ICoreWebView2NewWindowRequestedEventArgs: get_Uri is slot 3,
            # put_Handled is slot 6 (get_Uri, put_NewWindow, get_NewWindow)
            uri = None
            if _com_method(args, 3, _P_HR_PPV)(args, ctypes.byref(out)) == S_OK:
                uri = _read_cotaskmem_wstr(out.value)
            _com_method(args, 6, _P_HR_INT)(args, 1)
            if uri:
                fn(uri)

        return self._add_event(
            self._ADD_NEW_WINDOW_REQUESTED, IID_NewWindowRequestedEventHandler,
            _event,
        )


# --- loader ------------------------------------------------------------------
_loader = None


def _load_loader(loader_path: str):
    global _loader
    if _loader is None:
        _loader = ctypes.WinDLL(loader_path)
        _loader.CreateCoreWebView2EnvironmentWithOptions.argtypes = [
            ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_void_p, ctypes.c_void_p,
        ]
        _loader.CreateCoreWebView2EnvironmentWithOptions.restype = HRESULT
        _loader.GetAvailableCoreWebView2BrowserVersionString.argtypes = [
            ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_void_p),
        ]
        _loader.GetAvailableCoreWebView2BrowserVersionString.restype = HRESULT
    return _loader


def runtime_version(loader_path: str) -> str | None:
    """Version of the installed evergreen WebView2 runtime, or None."""
    if not os.path.isfile(loader_path):
        return None
    try:
        dll = _load_loader(loader_path)
        out = ctypes.c_void_p()
        hr = dll.GetAvailableCoreWebView2BrowserVersionString(
            None, ctypes.byref(out)
        )
        if hr != S_OK:
            return None
        return _read_cotaskmem_wstr(out.value)
    except OSError:
        logger.exception("WebView2Loader.dll failed to load")
        return None


def create_environment(loader_path: str, user_data_dir: str, callback) -> None:
    """callback(WebView2Environment | None, hresult) via the message pump."""
    dll = _load_loader(loader_path)
    # Qt has already initialized COM on the UI thread; S_FALSE /
    # RPC_E_CHANGED_MODE from this defensive call are both fine
    _ole32.CoInitializeEx(None, 2)  # COINIT_APARTMENTTHREADED
    os.makedirs(user_data_dir, exist_ok=True)

    def _done(hr, ptr):
        env = WebView2Environment(ptr, add_ref=True) if hr == S_OK and ptr else None
        callback(env, hr)

    handler = ComCallback(IID_EnvironmentCompletedHandler, INVOKE_HR_PTR, _done)
    hr = dll.CreateCoreWebView2EnvironmentWithOptions(
        None, user_data_dir, None, handler.ptr
    )
    if hr != S_OK:
        handler.dispose()
        callback(None, hr)
