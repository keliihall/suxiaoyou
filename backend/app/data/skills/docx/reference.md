# DOCX reference

## Inspect a document

```python
from docx import Document

doc = Document("input.docx")
for index, paragraph in enumerate(doc.paragraphs):
    print(index, paragraph.style.name, paragraph.text)
for index, table in enumerate(doc.tables):
    print("table", index, len(table.rows), len(table.columns))
```

## Create a simple document

```python
from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Cm, Pt

doc = Document()
section = doc.sections[0]
section.top_margin = Cm(2.5)
section.bottom_margin = Cm(2.5)
section.left_margin = Cm(2.8)
section.right_margin = Cm(2.8)

title = doc.add_heading("Report title", level=0)
title.alignment = WD_ALIGN_PARAGRAPH.CENTER
doc.styles["Normal"].font.size = Pt(11)
doc.add_paragraph("Summary text")
doc.save("report.docx")
```

## Validation

Reopen the file and check the expected structure. A DOCX is also a ZIP archive;
`zipfile.ZipFile(path).testzip()` should return `None` for a structurally intact
archive. Visual fidelity still requires Word, WPS Office, or LibreOffice.
