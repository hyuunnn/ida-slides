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
- `marp -w` rewrites the HTML on save → IDA reloads in ~200ms

---

## How to use

1. Author Markdown with the usual Marp directives
2. In IDA: `Ctrl+Shift+M` → pick `deck.md` (built-in slide viewer)
3. Or, with QtWebEngine installed, render full Marp themes:

   ```sh
   marp -w deck.md -o deck.html
   ```

   and open `deck.html` instead

---

## Clickable IDA references

Write `@` followed by any IDA name or address — it becomes a link
that jumps the disassembly view:

- Function by name: @main or @sub_401000
- Raw address: @0x401000
- Works inline in code too: `call @sub_401000`

Unknown names render dimmed instead of linked: @no_such_name_here

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
