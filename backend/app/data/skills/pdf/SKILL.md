---
name: pdf
description: Read, combine, split, create, and fill PDF files using redistributable Python tooling.
license: Apache-2.0
---

# PDF workflow

Use `pypdf` for page-level manipulation and form fields, `pdfplumber` for text
and table inspection, and ReportLab for new PDFs. Choose the smallest tool that
fits the task.

## Procedure

1. Preserve the original file and write to a separate output path.
2. Inspect page count, page sizes, metadata, encryption, and form fields before
   modifying the file.
3. For extraction, record page numbers and distinguish machine-readable text
   from scanned pages. OCR is a separate operation and must be reported.
4. For generated PDFs, use embedded or standard fonts that cover all required
   characters, stable margins, and explicit page breaks.
5. Reopen the output, confirm its page count, and extract representative text.
   Render pages when a renderer is available and visually inspect them.
6. Report encryption, unsupported annotations, signatures, or forms that could
   not be preserved.

## Safety

- Never remove a password or digital signature without authorization.
- Editing a signed PDF normally invalidates its signature; warn before writing.
- Do not claim that extracted text preserves the visual reading order unless it
  has been checked.
- Avoid rasterizing a document unless the user accepts loss of selectable text.

See `reference.md` and `forms.md` for examples.
