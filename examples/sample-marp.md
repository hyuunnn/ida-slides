---
marp: true
theme: default
paginate: true
---

# ida-slides

Marp slides inside an IDA Pro tab.

---

## Why

- No more Alt-Tab between IDA and Keynote during a live demo
- Slides dock next to Pseudocode / IMPORTS / Hex View
- Edit the deck in your editor; ida-slides re-renders and reloads on save

---

## How to use

1. Author Markdown with the usual Marp directives
2. In IDA: `Ctrl+Shift+M` → pick this `.md`
3. On macOS it renders with the real marp CLI in an embedded WKWebView;
   just save in your editor and the docked slide reloads in place

---

## Clickable IDA references

Write `@` followed by any IDA name or address — it becomes a link
that jumps the disassembly view:

- Function by name: @main or @sub_401000
- Raw address: @0x401000
- Works inline in code too: `call @sub_401000`
- Add `:N` to land on a pseudocode line: @main:12

Unknown names still look like links — clicking reports to IDA's Output
window, and the toolbar lint flags them: @no_such_name_here

---

## Embedded pseudocode

Write `@name[a:b]` and ida-slides drops the decompiled lines a–b right
into the slide — refreshed from the IDB every time you save:

@main[1:8]

Use `@name[7]` for a single line, `@name[]` for the whole function, or
`@name[1:8@5]` to mark line 5 with `►`.

---

## Bespoke.js shortcuts

| Key            | Action            |
|----------------|-------------------|
| `→` / PgDown   | Next slide        |
| `←` / PgUp     | Previous slide    |
| `Home` / `End` | First / last      |
| `f`            | Fullscreen toggle |
| `o`            | Slide overview    |

---

## Thanks

That's it. Have fun reversing.
