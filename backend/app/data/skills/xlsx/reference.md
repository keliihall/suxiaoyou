# XLSX reference

## Inspect a workbook

```python
from openpyxl import load_workbook

workbook = load_workbook("input.xlsx", data_only=False, read_only=False)
print(workbook.sheetnames)
for sheet in workbook.worksheets:
    print(sheet.title, sheet.max_row, sheet.max_column)
```

## Update cells while preserving formulas

```python
from openpyxl import load_workbook

workbook = load_workbook("input.xlsx", data_only=False)
sheet = workbook["Summary"]
sheet["B2"] = "Reviewed"
sheet["C2"] = "=SUM(C3:C20)"
sheet["C2"].number_format = "#,##0.00"
workbook.save("output.xlsx")
```

## Validate

Reopen the saved file and confirm important formulas and styles. To inspect
cached results, open a separately recalculated copy with `data_only=True`.
Formula calculation requires an external spreadsheet engine.
