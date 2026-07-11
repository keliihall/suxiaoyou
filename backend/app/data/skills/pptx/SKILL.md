---
name: pptx
description: Create, inspect, and make practical edits to PowerPoint .pptx presentations with python-pptx.
license: Apache-2.0
---

# PowerPoint workflow

Use `python-pptx` for ordinary slide creation and edits. Start from a user
template whenever brand fidelity matters.

## Procedure

1. Preserve the source presentation and select a separate output path.
2. Inspect slide sizes, layouts, titles, text, notes availability, tables,
   charts, and images before editing.
3. Reuse the presentation's existing layouts and theme. When creating from
   scratch, choose one aspect ratio and a small, consistent type scale.
4. Keep one message per slide. Use short headings and readable body text;
   prefer charts or diagrams only when they clarify the evidence.
5. Save and reopen the file with `python-pptx`. Verify slide count, titles,
   relationships, and referenced media.
6. Ask for visual review in PowerPoint, WPS Office, or LibreOffice. The library
   does not render slides and cannot prove pixel-perfect layout.

## Safety and fidelity

- Avoid modifying macros, embedded objects, unsupported animations, or complex
  charts unless the user accepts possible loss.
- Do not replace a template's theme merely to simplify implementation.
- Check for text overflow, low contrast, tiny fonts, and clipped images.
- Keep all factual claims traceable to the supplied material.

See `reference.md` for basic inspection and creation patterns.
