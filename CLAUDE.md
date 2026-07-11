# CLAUDE.md — ida-slides

## What this project is

An IDA Pro plugin (IDA 9.2+, Python) that renders real **Marp** or
**Slidev** slide decks inside a dockable IDA tab, for presenting reverse
-engineering work live. The deck and the IDB are bridged both ways:
`@name` tokens in slides drive IDA (jumps, embedded pseudocode), and a
right-click action copies references from IDA back into deck syntax.

The core concept is non-negotiable: **the deck lives in an IDA docking
tab**, side by side with disassembly/pseudocode. Designs that move
rendering into an external browser window were considered and rejected.

## Implemented features

- `@name` / `@0xADDR` → clickable links that jump the disassembly view;
  `@name:N` opens the Hex-Rays pseudocode at line N.
- `@name[a:b]` / `[7]` / `[]` / `[a:b@N]` → embeds decompiled lines into
  the slide as a code block, read live from the IDB on every save;
  `@N` marks one line with `►`.
- Hover preview: mousing over any `@` link shows a decompiled excerpt
  tooltip without leaving the slide.
- Copy @reference: right-click in disasm/pseudocode/hex view copies the
  token for that spot (`@name`, `@name:line`, or a selection as
  `@name[lo:hi]`). Names the token grammar can't re-parse (ObjC
  selectors, demangled C++) fall back to `@0xADDR` so the token always
  works.
- Deck lint: every load/save resolves all `@` tokens against the open
  IDB; unresolved ones show as `⚠ N unresolved @ref(s)` in the toolbar
  (tooltip lists token + slide number, details in Output). It is a
  *resolution* checker, not a syntax checker.
- Live reload: a debounced QFileSystemWatcher survives atomic-rename
  saves (VS Code, marp CLI) by re-adding the path; gives up after ~5s if
  the file is truly gone.
- Engine detection per deck: `ida-slides-engine:` front-matter override →
  `marp:` key (value respected, `marp: false` ≠ marp) → Slidev-specific
  front-matter keys (if the slidev CLI exists) → default marp.
- Focus preservation: jumps never steal keyboard focus from the deck, so
  arrow keys keep driving slides (details below).

## Architecture

```
ida_slides_entry.py      plugin entry (env gate) → ida_slides.py
ida_slides.py            action/menu registration (Ctrl+Shift+M)
presenter_form.py        dockable PluginForm: toolbar, webkit renderer,
                         file watcher wiring, lint display
webkit_view.py           the one renderer: native WKWebView via PyObjC,
                         marp/slidev CLI pipeline, injected USER_JS,
                         JS↔Python message bridge
ida_links.py             @token grammar (TOKEN_RE / JS_TOKEN_RE),
                         resolution, jumps
deck_preprocess.py       embed expansion, hover-preview text, deck lint
marp_markdown.py         deck-structure single source: front-matter
                         boundary, fence tracking (iter_fenced), slide
                         splitting — engine detection, embed expansion
                         and lint all build on it
copy_ref.py              Copy @reference context-menu action
file_watcher.py          debounced, rename-surviving file watcher
```

Render pipeline (macOS/.md): deck.md → `deck_preprocess.expand_embeds`
(decompiles `@name[a:b]` tokens) → hidden `.name.ida-slides.md` → marp
CLI (QProcess) → `.name.ida-slides.html` → WKWebView. Slidev decks run a
local dev server instead and rely on Vite HMR. `USER_JS` (a WKUserScript)
linkifies `@tokens` in the rendered DOM and posts click/preview messages
to Python via a WKScriptMessageHandler.

## Design decisions & tradeoffs

- **WKWebView over QtWebEngine (macOS).** IDA's bundled PySide6 has no
  QtWebEngine, and pip's QtWebEngine wheels can ABI-clash with IDA's
  bundled Qt. The system WebKit is free, native, and GPU-accelerated.
  Tradeoff: the plugin is **macOS-only** (owner's explicit call — don't
  invest in cross-platform work unless asked). There are NO fallback
  renderers: without WKWebView or the deck's engine CLI (marp/slidev),
  decks simply don't render (a warning / status message says why).
- **PyObjC crash safety (documented in webkit_view.py header).** A
  Python exception escaping a PyObjC delegate aborts IDA, and PyObjC
  cannot call WebKit completion-handler *blocks* at all. Therefore no
  delegate method that receives a block is ever implemented — click
  routing uses WKUserScript + postMessage instead of navigation
  delegates. Keep it that way.
- **Save-time batch rendering, not incremental.** Every save re-runs
  the preprocess; marp runs as a persistent `-w` watcher (one per deck,
  stopped on cleanup / file switch / engine switch) that re-renders when
  the prepared md is rewritten. The view reloads when marp logs its
  render-complete line (`[ INFO ] … => <out>`) on stderr — NOT on mtime,
  which can't tell this save's render from an earlier one and can be seen
  mid-write. A 15s timeout only shows a "taking longer" heads-up; a dead
  watcher is caught by `_on_marp_exit`. Tradeoff accepted for
  pixel-perfect themes; costs are blunted by the 200ms debounce,
  Hex-Rays' internal cfunc cache, slice-only tag_remove in
  `decompile_lines`, an output-diff guard, a single deck read per load
  (shared by detect_engine and _prepare_md), and a per-pass name cache
  in the lint. The diff guard sits
  intentionally AFTER `expand_embeds`: a same-content save is the
  documented gesture for refreshing embeds after an IDB rename, so
  identical input must not skip expansion. Status label policy: error
  messages only — no per-save "rendering…" text (the label popping in
  reflows the pane on every save). The 15s "taking longer" notice is the
  one allowed non-error message, since it fires only on an abnormal stall.
- **Focus invariants.** Verified mechanics, easy to regress:
  - `jumpto(ea, -1, 0)` (no UIJMP_ACTIVATE) repositions without taking
    focus but does NOT raise a buried tab; `activate_widget(w, False)`
    raises without focus.
  - `open_pseudocode(..., OPF_REUSE)` steals focus even when reusing a
    view — capture `get_current_widget()` before, restore synchronously
    after.
  - The `_position` caret-retry loop must use `activate_widget(ct, False)`.
- **Never hold a TWidget/vdui across a QTimer delay.** Stale SWIG
  pointers can hard-crash IDA (not catchable in Python). Re-resolve via
  `find_widget(title)` + `get_widget_vdui()` and compare `cfunc.entry_ea`
  before touching the viewer (see `_jump_to_pseudocode_line`).
- **@token linkify lives in one place** — `USER_JS` (WKWebView); the
  grammar is single-sourced from `ida_links._NAME_PATTERN` via
  `JS_TOKEN_RE`. (The former Python `linkify_html` and QtWebEngine
  `LINKIFY_JS` copies were deleted with the fallback renderers.)
  Behavioral quirk: the lint (`unresolved_refs`) trims trailing dots only
  while unresolvable; USER_JS trims unconditionally (no IDB access at
  render time).
- **Markdown parsing is a pragmatic subset, aligned with marp where it
  matters:** fences follow the CommonMark closing-length rule; a `---`
  directly under text is a setext H2, not a slide break. `split_slides`
  drives the lint's slide numbers, so divergence from marp shows up as
  off-by-one lint numbers.
- **Removed features (owner decisions — do not re-add):**
  - `@!` presenter-follow (auto-jump when a slide becomes visible):
    removed with its toolbar toggle. Legacy `@!name` tokens render as
    dead text and are invisible to the lint — known and accepted.
  - Landing flash (tinting the pseudocode line a `:N` jump arrived at).
  - Fallback renderers (`renderers.py`: built-in QTextBrowser viewer and
    the thin QtWebEngine .html view), removed 2026-07 along with
    `linkify_html`/`LINKIFY_JS`/`make_href`/`name_from_url`. marp/slidev
    via WKWebView is the only supported path — no degraded rendering.
- **Accepted behaviors (not bugs):** unmapped raw-hex refs
  (`@0xDEADBEEF`) render as live-looking links and silently fail on
  click — the lint warning is considered sufficient. IDA's native
  Close/Float/Fullscreen dock tooltips stay; only the unresolved-refs
  warning label carries a plugin tooltip.

## Working on the code

- The repo is symlinked at `~/.idapro/plugins/ida-slides`; a running IDA
  loads this working tree directly.
- A live IDA is usually reachable via the `ida-pro-mcp` MCP server
  (`py_eval`) — verify UI/focus/crash behavior empirically there rather
  than reasoning about it. Reload flow: `importlib.reload` the changed
  modules, then reload `presenter_form` and reopen the form. Gotchas:
  - Reload does not delete removed attributes; injected-JS changes need
    the form reopened (the user script is baked in at webview creation).
  - Close and re-Show the form in SEPARATE py_eval calls — widget close
    is async and a same-caption collision makes `Show()` fail silently.
  - Too many reload/reopen cycles can orphan the dock tab (unclosable
    ghost); only an IDA restart clears it.
  - IDA's dock chain has no QDockWidget — never walk Qt parents calling
    `close()` without stopping before QMainWindow.
- `py_eval` uses exec-with-dict scoping: module-level names are invisible
  inside nested `def`/`lambda` — bind them via default arguments.
- Focus can be tested with IDA in the background via
  `mainwindow.focusWidget()`; `get_current_widget()` returns None when
  the app is inactive.
- Regression tests live in `tests/test_in_ida.py` (run inside IDA — see
  README "Tests"). Pure-logic checks always run; DB-dependent ones pick a
  function from the open IDB. Add a case here when fixing a logic bug.
- Owner tests UI changes himself and reports back; commit per feature
  batch (English, imperative), push only when asked.

## Outstanding cleanups

- (none currently — the three-copy linkify item resolved itself when the
  fallback renderers were deleted; only USER_JS remains)
