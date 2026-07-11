# PPTX reference

## Inspect slide text

```python
from pptx import Presentation

deck = Presentation("input.pptx")
print("slides", len(deck.slides), "size", deck.slide_width, deck.slide_height)
for number, slide in enumerate(deck.slides, start=1):
    texts = [shape.text for shape in slide.shapes if hasattr(shape, "text")]
    print(number, texts)
```

## Create a simple deck

```python
from pptx import Presentation
from pptx.util import Pt

deck = Presentation()
slide = deck.slides.add_slide(deck.slide_layouts[1])
slide.shapes.title.text = "Decision summary"
body = slide.placeholders[1].text_frame
body.text = "First supporting point"
body.paragraphs[0].font.size = Pt(24)
deck.save("presentation.pptx")
```

Reopen the output and verify its structure. Use an Office application for
visual inspection because `python-pptx` does not include a rendering engine.
