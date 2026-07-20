---
name: xlsx
description: Inspect, analyze, create, and update Excel workbooks with openpyxl and pandas.
license: Apache-2.0
---

# Spreadsheet workflow

Use the built-in `office` tool for its supported declarative operations. Use
`openpyxl` when advanced formulas, styles, charts, or workbook structure
matter, and `pandas` for table-shaped analysis before writing the result back
carefully.

If the built-in `office` tool reports that its authoritative Office v1.1
runtime is unavailable, do not retry it. Use `code_execute` with the bundled
`openpyxl` package, save and reopen the workbook in the same call, and verify
the requested sheets and representative cells. Do not create a virtual
environment or install packages.

When its runtime is available, use the built-in `office` tool first for basic
sheet creation/deletion, row appends, cell updates, number formats, fonts, and
solid fills. It is available on macOS, Windows, and Linux, stays inside the
selected workspace, versions an existing destination, and validates a
temporary XLSX before atomic installation. It stores formulas but never
recalculates their results. Do not write a Python or shell helper for
operations covered by `office`.

## Procedure

1. Keep an untouched source copy and choose an explicit output path.
2. Inspect sheet names, used ranges, merged cells, tables, formulas, hidden
   sheets, data validation, and named ranges before editing.
3. Preserve formulas unless the user requests values. Load with
   `data_only=False` when editing formulas.
4. Apply number formats deliberately: dates, percentages, currency, and plain
   identifiers are not interchangeable.
5. Save and reopen the workbook. Verify sheet names, dimensions, formulas, and
   representative values.
6. State whether formulas were recalculated. `openpyxl` writes formulas but
   does not calculate them; Excel, WPS Office, or LibreOffice may be needed.

## Safety and fidelity

- Do not convert identifiers with leading zeroes into numbers.
- Do not overwrite macros in `.xlsm`; load and save with `keep_vba=True` when
  preservation is required, and still warn that verification is necessary.
- Avoid deleting hidden sheets, names, validations, or external links without
  an explicit request.
- Never report calculated results from stale formula caches as newly computed.

See `reference.md` for concise patterns.
