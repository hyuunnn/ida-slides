---
theme: default
transition: slide-left
title: ida-slides Slidev sample
---

# ida-slides × Slidev

Slidev decks inside an IDA Pro tab.

---

## How this works

- This deck has Slidev front-matter keys (`transition:` …), so ida-slides
  starts a local `slidev` dev server and renders it in the docked tab
- Vite HMR applies your edits on save — no reload needed
- Force an engine with `ida-slides-engine: slidev` (or `marp`) in front matter

---

## Clickable IDA references

Same as the Marp engine — `@` + any IDA name or address jumps the view:

- Function by name: @main or @sub_401000
- Raw address: @0x401000
- Inline code too: `call @sub_401000`
- Pseudocode line: @main:12

---

## Embedded pseudocode

`@name[a:b]` embeds decompiled lines a–b, refreshed on every save
(Vite HMR applies it instantly):

@main[1:8]
