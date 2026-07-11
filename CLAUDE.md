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
marp_presenter_entry.py  plugin entry (env gate) → marp_presenter.py
marp_presenter.py        action/menu registration (Ctrl+Shift+M)
presenter_form.py        dockable PluginForm: toolbar, renderer choice,
                         file watcher wiring, lint display
webkit_view.py           macOS renderer: native WKWebView via PyObjC,
                         marp/slidev CLI pipeline, injected USER_JS,
                         JS↔Python message bridge
renderers.py             fallbacks: built-in QTextBrowser slide viewer
                         and a thin QtWebEngine view (pre-rendered .html)
ida_links.py             @token grammar (TOKEN_RE), resolution, jumps,
                         linkify (Python + LINKIFY_JS for QtWebEngine)
deck_preprocess.py       embed expansion, hover-preview text, deck lint
marp_markdown.py         front matter / slide splitting for the built-in
                         viewer and for lint slide numbers
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
  Tradeoff: the plugin is **macOS-only for now** (owner's explicit call —
  don't invest in cross-platform work unless asked). Other platforms get
  reduced fallbacks: QtWebEngine for pre-rendered .html, built-in
  QTextBrowser for .md.
- **PyObjC crash safety (documented in webkit_view.py header).** A
  Python exception escaping a PyObjC delegate aborts IDA, and PyObjC
  cannot call WebKit completion-handler *blocks* at all. Therefore no
  delegate method that receives a block is ever implemented — click
  routing uses WKUserScript + postMessage instead of navigation
  delegates. Keep it that way.
- **Save-time batch rendering, not incremental.** Every save re-runs
  the preprocess; marp runs as a persistent `-w` watcher (one per deck,
  stopped on cleanup / file switch / engine switch) that re-renders when
  the prepared md is rewritten, and the view reloads when the output
  html's mtime advances. Tradeoff accepted for pixel-perfect themes;
  costs are blunted by the 200ms debounce, Hex-Rays' internal cfunc
  cache, slice-only tag_remove in `decompile_lines`, and an output-diff
  guard. The diff guard sits intentionally AFTER `expand_embeds`: a
  same-content save is the documented gesture for refreshing embeds
  after an IDB rename, so identical input must not skip expansion.
  Status label policy: error messages only — no transient "rendering…"
  text (the label popping in reflows the pane on every save).
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
- **@token linkify logic exists in three copies** — Python
  `linkify_html`, `LINKIFY_JS` (QtWebEngine), `USER_JS` (WKWebView). The
  grammar itself is single-sourced (`ida_links.JS_TOKEN_RE` is generated
  from `_NAME_PATTERN` and substituted into both JS blobs), but the
  surrounding logic (escaping, trailing-dot trim, email guard as a
  prev-char check) is still hand-mirrored. Behavioral quirk: Python trims
  trailing dots only when unresolvable; JS trims unconditionally (no IDB
  access at render time).
- **Markdown parsing is a pragmatic subset, aligned with marp where it
  matters:** fences follow the CommonMark closing-length rule; a `---`
  directly under text is a setext H2, not a slide break. `split_slides`
  drives both the built-in viewer and the lint's slide numbers, so
  divergence from marp shows up as off-by-one lint numbers.
- **Removed features (owner decisions — do not re-add):**
  - `@!` presenter-follow (auto-jump when a slide becomes visible):
    removed with its toolbar toggle. Legacy `@!name` tokens render as
    dead text and are invisible to the lint — known and accepted.
  - Landing flash (tinting the pseudocode line a `:N` jump arrived at).
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
- Owner tests UI changes himself and reports back; commit per feature
  batch (English, imperative), push only when asked.

## Outstanding cleanups

- Merging the three linkify implementations' *logic* (the grammar is
  already single-sourced) — worth doing whenever the injected JS gets
  its next substantial change.
