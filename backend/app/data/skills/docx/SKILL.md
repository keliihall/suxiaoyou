---
name: docx
description: Create, inspect, and make practical edits to Microsoft Word .docx files with open-source Python libraries.
license: Apache-2.0
---

# Word document workflow

Use this skill when the primary input or output is a `.docx` file. The
built-in `office` tool is the default for its supported declarative operations;
use `python-docx` only for advanced images, sections, headers, and footers.

If the built-in `office` tool reports that its authoritative Office v1.1
runtime is unavailable, treat that as a capability signal, not a transient
error. Do not retry the tool. Continue with `code_execute` and the bundled
`python-docx` package. Do not create a virtual environment or install packages.

When its runtime is available, use the built-in `office` tool first for
ordinary paragraphs, headings, tables, page breaks, workspace-local images,
appends, and exact text replacements. It is available on macOS, Windows, and
Linux, stays inside the selected workspace, versions an existing destination,
and validates a temporary DOCX before atomic installation. Do not write a
Python or shell helper for operations covered by `office`.

## Procedure

1. Confirm the source file and the requested output path. Never overwrite the
   only copy of a user document unless the user explicitly requests it.
2. Inspect the existing document before editing: paragraph text and styles,
   table dimensions, section sizes, margins, headers, and footers.
3. Preserve the existing style system when updating a document. Use a new,
   consistent style set only when creating a document from scratch.
4. Keep content and presentation separate: build the outline and facts first,
   then apply typography, spacing, tables, and images.
5. Save to a new `.docx`, reopen it with `python-docx`, and verify that the
   expected paragraphs, tables, and relationships are present.
6. Report the final absolute path and any limitations that need visual review
   in Word, WPS Office, or LibreOffice.

## Safety and fidelity

- Do not silently remove macros, comments, tracked changes, embedded objects,
  or unsupported XML. `python-docx` does not preserve every Word feature.
- For a complex template, make the smallest possible change and keep a backup.
- Use explicit page size and margins for newly created deliverables.
- Avoid using tables solely for decorative layout when normal paragraphs and
  tab stops are sufficient.
- Do not claim pixel-perfect rendering without opening the result in an Office
  renderer.

See `reference.md` for concise examples.
