# PDF form fields

Inspect fields before writing:

```python
from pypdf import PdfReader

reader = PdfReader("form.pdf")
for name, field in (reader.get_fields() or {}).items():
    print(name, field.get("/FT"), field.get("/V"))
```

Fill fields on a copy:

```python
from pypdf import PdfReader, PdfWriter

reader = PdfReader("form.pdf")
writer = PdfWriter()
writer.append(reader)
values = {"full_name": "Example Person"}
for page in writer.pages:
    writer.update_page_form_field_values(page, values, auto_regenerate=True)
with open("filled-form.pdf", "wb") as output:
    writer.write(output)
```

Field names differ between documents. Confirm the resulting values and visual
appearance in a PDF viewer. Filling or rewriting a signed form can invalidate
its digital signature.
