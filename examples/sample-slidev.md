---
theme: default
transition: slide-left
title: ida-slides Slidev sample
---

# ida-slides × Slidev

Slidev decks inside an IDA Pro tab.

---

## Why

- No more Alt-Tab between IDA and Keynote during a live demo
- Slides dock next to Pseudocode / IMPORTS / Hex View
- Edit the deck in your editor; Vite HMR applies it on save — no reload

---

## How to use

1. Author Markdown with the usual Slidev syntax
2. In IDA: `Ctrl+Shift+M` → pick this `.md`
3. This deck has Slidev front-matter keys (`transition:` …), so ida-slides
   starts a local `slidev` dev server and renders it in the docked tab
4. Force an engine with `ida-slides-engine: slidev` (or `marp`) in front matter

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
into the slide — refreshed from the IDB every time you save
(Vite HMR applies it instantly):

@main[1:8]

Use `@name[7]` for a single line, `@name[]` for the whole function, or
`@name[1:8@5]` to mark line 5 with `►`.

---

## Slidev shortcuts

| Key            | Action            |
|----------------|-------------------|
| `→` / `Space`  | Next slide        |
| `←`            | Previous slide    |
| `o`            | Slide overview    |
| `f`            | Fullscreen toggle |
| `d`            | Dark mode toggle  |

---

## Thanks

That's it. Have fun reversing.
