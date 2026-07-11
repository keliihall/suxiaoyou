# PDF reference

## Inspect and extract

```python
from pypdf import PdfReader

reader = PdfReader("input.pdf")
print("pages", len(reader.pages), "encrypted", reader.is_encrypted)
for number, page in enumerate(reader.pages, start=1):
    print(number, page.mediabox, (page.extract_text() or "")[:500])
```

## Merge PDFs

```python
from pypdf import PdfReader, PdfWriter

writer = PdfWriter()
for path in ["part-a.pdf", "part-b.pdf"]:
    for page in PdfReader(path).pages:
        writer.add_page(page)
with open("combined.pdf", "wb") as output:
    writer.write(output)
```

## Create a PDF with ReportLab

```python
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen.canvas import Canvas

canvas = Canvas("output.pdf", pagesize=A4)
canvas.drawString(72, A4[1] - 72, "Report")
canvas.save()
```

Always reopen the result and verify page count and representative content.
