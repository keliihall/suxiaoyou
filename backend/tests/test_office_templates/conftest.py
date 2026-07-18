from __future__ import annotations

import pytest

from tests.test_office_templates.helpers import (
    make_docx_template,
    make_pptx_template,
    make_xlsx_template,
)


@pytest.fixture(scope="session")
def docx_template_bytes() -> bytes:
    return make_docx_template()


@pytest.fixture(scope="session")
def xlsx_template_bytes() -> bytes:
    return make_xlsx_template()


@pytest.fixture(scope="session")
def pptx_template_bytes() -> bytes:
    return make_pptx_template()
