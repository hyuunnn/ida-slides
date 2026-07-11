# Windows renderer — static verification notes (2026-07-12)

Static audit of the WebView2 stack done from the mac side (no Windows
machine available). Four independent audits: vtable/GUID cross-check
against the official SDK header, COM object lifecycle, DeckViewBase hook
conformance (mac implementation as reference), and Windows platform
branches. **Verdict: no crash-class defect found; the findings below are
leaks, diagnosability gaps, and install-layout edge cases.**

**Windows-side reconciliation (2026-07-12, same day):** an independent
multi-agent review on the Windows machine converged on findings 1, 5, 6
and 7 and fixed them in d0002a8 (verified live: `com_handlers_drained`
regression check added to the smoke test); finding 2 was tested live and
REFUTED (Korean+space deck path renders with live links under the current
PrettyDecoded URL — WebView2's Navigate tolerates it). Findings 3 and 4
were fixed in the follow-up (tree-kill + node probing; orphan reproduced
and fix verified live). Open: 8, plus 9's scoop-glob and license-provenance
nits. Per-item status notes inline below.

## Proven statically (do not re-verify)

- Every GUID and vtable slot in `webview2_com.py` **as of e72ff31**
  matches `WebView2.h` from the official Microsoft.Web.WebView2 NuGet
  package (1.0.902.49) byte-for-byte — including `put_Bounds`(RECT by
  value), the EventRegistrationToken ABI, and all handler Invoke shapes.
  (The `get_IsSuccess` slot-3 read added later in d0002a8 postdates this
  audit; it was verified against the same header during the Windows
  review and is exercised by the standalone harness's navigation checks.)
- Callback objects cannot be GC'd while native code holds them (vtable
  array, ffi closures, and the Python object are all pinned).
- IUnknown discipline (QI riid compare, AddRef on success, E_NOINTERFACE
  + NULL out), LPWSTR out-params CoTaskMemFree'd, `[in]` strings not
  freed, STA/pump assumptions sound (no blocking waits on COM
  completions; everything defers out of COM frames via singleShot(0)).
- All 9 DeckViewBase hooks implemented with mac-equivalent semantics;
  the JS→Python bridge shape (chrome.webview.postMessage → WebMessageAsJson
  → dict{type,name,line} → dispatch_page_message) matches the mac path.

## Findings (fix on the Windows machine, smoke test in hand)

1. **[FIXED d0002a8]** **Handler refcount floor — unbounded leak** (`webview2_com.py:137`).
   ComCallback keeps its construction reference; WebView2 pairs its own
   AddRef/Release (standard `[in]` semantics, cf. WRL `Callback<>`), so
   counts bottom out at 1 and nothing ever leaves `_LIVE`. Every
   ExecuteScript (hover, save-poll) leaks a handler; closed views are
   never freed. Fix: release the construction ref after a successful
   call (one-shot completed handlers can release in `_invoke`).
   *Refcount changes are crash-sensitive — fix with
   `test_webview2_standalone.py` runs between edits.*
2. **[REFUTED — live test]** **File URLs not percent-encoded** (`webview2_view.py:275`).
   `QUrl.fromLocalFile(...).toString()` default emits raw spaces/Korean;
   use `toString(QUrl.ComponentFormattingOption.FullyEncoded)`. A deck
   under a Korean/space path may navigate to a blank pane silently.
3. **[FIXED — tree-kill]** **pnpm/yarn installs orphan the renderer on
   kill** (`deck_view.py:348`). `_spawn_spec`'s node_modules entry
   resolution only knows npm-prefix / nvm-windows layouts; pnpm/yarn fall
   back to the .cmd shim, so `QProcess.kill()` reaps only cmd.exe and the
   node child survives (re-rendering forever, also after IDA exits). Also:
   on Python ≤3.11 `shutil.which` can return the extensionless sh shim,
   which QProcess cannot start at all.
   *Fix: `_kill_tool_proc` kills the process TREE via taskkill /T on
   Windows (verified live: shim-spawned `marp -w` orphaned its node under
   plain kill(), none under tree-kill, 172ms); the shim fallback now logs;
   extensionless shims reroute to the .cmd twin like .ps1.*
4. **[FIXED]** **npm-prefix layout has no node next to the shim**
   (`deck_view.py:787`). `%APPDATA%\npm` holds marp.cmd but node.exe
   lives in `%ProgramFiles%\nodejs`; with node off PATH both spawn
   strategies fail as a bare 'marp CLI failed to start'. Probe
   `%ProgramFiles%\nodejs` for node and say WHICH piece is missing.
   *Fix: `_find_node` probes ProgramFiles / NVM_SYMLINK past PATH, its
   directory is injected into the tool PATH so shims work, and a missing
   node now logs exactly that before the generic failure.*
5. **[FIXED d0002a8]** **`_stop_slidev` freezes the UI ~1.5s on Windows**
   (`deck_view.py:919`). `terminate()` posts WM_CLOSE, which a console
   node ignores, so `waitForFinished(1500)` always times out before
   kill(). On `_IS_WIN`, skip terminate and kill directly (verify vite's
   esbuild child exits when its service pipe closes).
6. **[FIXED d0002a8]** **Late attach after declared failure** (`webview2_view.py:186`).
   `_attach_failed` doesn't bump `_attach_gen`, so a stalled attempt
   completing after the watchdog gave up still attaches, leaving a live
   view with `attach_failed` latched (next Open rebuilds it needlessly).
7. **[FIXED d0002a8 — retry path; the `:200` concurrent-overwrite case
   is moot while views are a singleton]** **Stalled/raced environments never released**
   (`webview2_view.py:181` retry path; `:200` concurrent-create
   overwrite) — browser processes + UDF lock linger for the session.
8. **`availability_error` misdiagnosis** (`webview2_view.py:87`):
   an existing-but-unloadable loader DLL (ARM64 host, AV block) is
   reported as 'runtime not installed'. Distinguish the OSError path.
9. Minor: scoop glob points at `scoop\shims` (npm globals live under
   `scoop\persist\nodejs\bin`); ~~`webview2_com.reload()` (slot 31) is
   dead code — delete or smoke-test it~~ (deleted in the 2nd-review
   cleanup); the DLL license file records no package version/arch
   provenance (note: extracted from Microsoft.Web.WebView2, x64 only).

## Smoke-test gaps (what `test_webview2_standalone.py` cannot catch)

- `MoveFocus` (slot 12) and `NotifyParentWindowPositionChanged`
  (slot 23) are never invoked by the smoke — their first-ever calls are
  a live @token click's focus restore and a live dock drag. Slot indices
  are verified against the header, but these two calls happen first in
  IDA.
- Shared-environment reuse (close form → reopen), the whole
  watchdog/retry/per-pid-UDF machinery, the slidev pipeline, the
  QFileSystemWatcher save loop, and every IDA-dependent flow.

## Live checklist (ordered by risk)

1. Click an @token: jump + focus back to deck (first MoveFocus); then
   drag/undock the pane (first NotifyParentWindowPositionChanged).
2. Close/reopen the form (shared-env reuse); second IDA instance
   attaches via per-pid UDF (~8s); stale `-<pid>` UDFs swept later.
3. Task Manager: no surviving `node.exe` after deck switch / engine
   switch / form close / IDA exit. Repeat with pnpm-installed marp —
   finding 3 predicts a leak there; confirm.
4. Live-reload from VS Code and Notepad++ (ReplaceFile saves); CRLF+BOM
   deck: engine override honored, lint numbers right.
5. Tool discovery with IDA launched from the Start Menu: npm-prefix,
   nvm-windows (newest version wins), node removed from PATH
   (finding 4's misleading error expected).
6. Slidev deck E2E; time the deck-switch stall (finding 5, ~1.5s);
   check for orphaned node/esbuild.
7. ~~Korean-named and space-containing deck paths (finding 2)~~ — done,
   REFUTED (see status header); still live: plugin dir as NTFS junction
   (loader DLL must still load).
8. Hover preview, embed refresh after IDB rename, lint, copy @reference.
9. Deck-switch sibling cleanup (no PermissionError leftovers from
   Windows file locking).
10. Per-monitor DPI (the known-unverified item), resize after load,
    external links open in the system browser.
