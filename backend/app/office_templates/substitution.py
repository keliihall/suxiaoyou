"""Placeholder discovery and text-only OOXML substitution."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping, Sequence
from io import BytesIO
from typing import Any

from lxml import etree

from app.office_templates.errors import TemplateContractError, TemplateSecurityError
from app.office_templates.models import OfficeTemplateFormat


TOKEN_PATTERN = re.compile(r"\{\{([A-Za-z][A-Za-z0-9_.-]{0,63})\}\}")
_XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"
_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_S = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_A = "http://schemas.openxmlformats.org/drawingml/2006/main"


def is_substitutable_part(part_name: str, format_name: OfficeTemplateFormat) -> bool:
    if format_name == "docx":
        return (
            part_name == "word/document.xml"
            or re.fullmatch(r"word/header[^/]*\.xml", part_name) is not None
            or re.fullmatch(r"word/footer[^/]*\.xml", part_name) is not None
        )
    if format_name == "xlsx":
        return part_name == "xl/sharedStrings.xml" or (
            re.fullmatch(r"xl/worksheets/[^/]+\.xml", part_name) is not None
        )
    return re.fullmatch(r"ppt/slides/slide[^/]*\.xml", part_name) is not None


def placeholder_counts(
    part_name: str,
    content: bytes,
    format_name: OfficeTemplateFormat,
) -> Counter[str]:
    root = _parse_xml(content, part_name)
    counts: Counter[str] = Counter()
    scopes = _text_scopes(root, part_name, format_name)
    _reject_delimiters_outside_scopes(root, scopes, part_name)
    for nodes in scopes:
        text = "".join(node.text or "" for node in nodes)
        matches = list(TOKEN_PATTERN.finditer(text))
        scrubbed = TOKEN_PATTERN.sub("", text)
        if "{{" in scrubbed or "}}" in scrubbed:
            raise TemplateContractError(
                f"Malformed Office template placeholder in {part_name}"
            )
        counts.update(match.group(1) for match in matches)
    return counts


def substitute_part(
    part_name: str,
    content: bytes,
    format_name: OfficeTemplateFormat,
    values: Mapping[str, str],
) -> tuple[bytes, Counter[str]]:
    """Replace placeholders inside supported text scopes without touching run style."""

    root = _parse_xml(content, part_name)
    changes: Counter[str] = Counter()
    scopes = _text_scopes(root, part_name, format_name)
    _reject_delimiters_outside_scopes(root, scopes, part_name)
    for nodes in scopes:
        text = "".join(node.text or "" for node in nodes)
        matches = list(TOKEN_PATTERN.finditer(text))
        scrubbed = TOKEN_PATTERN.sub("", text)
        if "{{" in scrubbed or "}}" in scrubbed:
            raise TemplateContractError(
                f"Malformed Office template placeholder in {part_name}"
            )
        if not matches:
            continue
        _replace_matches(nodes, matches, values)
        changes.update(match.group(1) for match in matches)
    if not changes:
        return content, changes
    return (
        etree.tostring(
            root,
            encoding="UTF-8",
            xml_declaration=True,
            pretty_print=False,
        ),
        changes,
    )


def validate_placeholder_values(
    required: Sequence[str],
    values: Mapping[str, object],
) -> dict[str, str]:
    if not isinstance(values, Mapping):
        raise TemplateContractError("placeholder values must be a mapping")
    required_set = set(required)
    provided_set = set(values)
    if any(not isinstance(key, str) for key in values):
        raise TemplateContractError("placeholder names must be strings")
    missing = sorted(required_set - provided_set)
    unknown = sorted(provided_set - required_set)
    if missing:
        raise TemplateContractError(
            "missing required placeholders: " + ", ".join(missing)
        )
    if unknown:
        raise TemplateContractError("unknown placeholders: " + ", ".join(unknown))
    normalized: dict[str, str] = {}
    total_length = 0
    for name in required:
        value = values[name]
        if not isinstance(value, str):
            raise TemplateContractError(f"placeholder {name} must be text")
        if len(value) > 100_000:
            raise TemplateContractError(f"placeholder {name} is too long")
        if "{{" in value or "}}" in value:
            raise TemplateContractError(
                f"placeholder {name} cannot contain template delimiters"
            )
        if any(
            ord(character) < 32 and character not in {"\t", "\n", "\r"}
            for character in value
        ):
            raise TemplateContractError(
                f"placeholder {name} contains invalid XML control characters"
            )
        total_length += len(value)
        if total_length > 1024 * 1024:
            raise TemplateContractError("placeholder values exceed the total text budget")
        normalized[name] = value
    return normalized


def _parse_xml(content: bytes, part_name: str) -> etree._Element:
    lowered = content[:4096].lower()
    if b"<!doctype" in lowered or b"<!entity" in lowered:
        raise TemplateSecurityError(f"DTD or entity declarations are forbidden: {part_name}")
    parser = etree.XMLParser(
        resolve_entities=False,
        no_network=True,
        load_dtd=False,
        remove_blank_text=False,
        remove_comments=False,
        huge_tree=False,
    )
    try:
        return etree.parse(BytesIO(content), parser).getroot()
    except (etree.XMLSyntaxError, ValueError) as exc:
        raise TemplateSecurityError(f"Invalid OOXML XML part: {part_name}") from exc


def _text_scopes(
    root: etree._Element,
    part_name: str,
    format_name: OfficeTemplateFormat,
) -> list[list[etree._Element]]:
    scopes: list[list[etree._Element]] = []
    if format_name == "docx":
        for paragraph in root.xpath(".//w:p", namespaces={"w": _W}):
            nodes = paragraph.xpath(".//w:t", namespaces={"w": _W})
            if nodes:
                scopes.append(nodes)
        return scopes
    if format_name == "xlsx":
        if part_name == "xl/sharedStrings.xml":
            containers = root.xpath(".//s:si", namespaces={"s": _S})
        else:
            containers = root.xpath(".//s:is", namespaces={"s": _S})
        for container in containers:
            nodes = container.xpath(".//s:t", namespaces={"s": _S})
            if nodes:
                scopes.append(nodes)
        return scopes
    for paragraph in root.xpath(".//a:p", namespaces={"a": _A}):
        nodes = paragraph.xpath(".//a:t", namespaces={"a": _A})
        if nodes:
            scopes.append(nodes)
    return scopes


def _replace_matches(
    nodes: Sequence[etree._Element],
    matches: Sequence[re.Match[str]],
    values: Mapping[str, str],
) -> None:
    original_texts = [node.text or "" for node in nodes]
    spans: list[tuple[int, int]] = []
    cursor = 0
    for text in original_texts:
        spans.append((cursor, cursor + len(text)))
        cursor += len(text)

    for match in reversed(matches):
        name = match.group(1)
        if name not in values:
            raise TemplateContractError(f"No value supplied for placeholder {name}")
        start_index, start_offset = _locate_offset(
            spans,
            match.start(),
            prefer_previous=False,
        )
        end_index, end_offset = _locate_offset(
            spans,
            match.end(),
            prefer_previous=True,
        )
        replacement = values[name]
        if start_index == end_index:
            current = nodes[start_index].text or ""
            _set_text(
                nodes[start_index],
                current[:start_offset] + replacement + current[end_offset:],
            )
            continue
        start_text = nodes[start_index].text or ""
        end_text = nodes[end_index].text or ""
        _set_text(nodes[start_index], start_text[:start_offset] + replacement)
        for index in range(start_index + 1, end_index):
            _set_text(nodes[index], "")
        _set_text(nodes[end_index], end_text[end_offset:])


def _reject_delimiters_outside_scopes(
    root: etree._Element,
    scopes: Sequence[Sequence[etree._Element]],
    part_name: str,
) -> None:
    allowed_text_nodes = {id(node) for scope in scopes for node in scope}
    for element in root.iter():
        if any(
            "{{" in attribute_value or "}}" in attribute_value
            for attribute_value in element.attrib.values()
        ):
            raise TemplateContractError(
                f"Placeholders are not allowed in OOXML attributes: {part_name}"
            )
        if id(element) not in allowed_text_nodes and element.text and (
            "{{" in element.text or "}}" in element.text
        ):
            raise TemplateContractError(
                f"Placeholders are not allowed outside Office text: {part_name}"
            )
        if element.tail and ("{{" in element.tail or "}}" in element.tail):
            raise TemplateContractError(
                f"Placeholders are not allowed outside Office text: {part_name}"
            )


def _locate_offset(
    spans: Sequence[tuple[int, int]],
    offset: int,
    *,
    prefer_previous: bool,
) -> tuple[int, int]:
    for index, (start, end) in enumerate(spans):
        if start <= offset < end or (
            prefer_previous and offset == end and end > start
        ):
            return index, offset - start
    if offset == 0 and spans:
        return 0, 0
    raise TemplateContractError("placeholder span cannot be mapped to OOXML text runs")


def _set_text(node: etree._Element, value: str) -> None:
    node.text = value
    if value[:1].isspace() or value[-1:].isspace():
        node.set(_XML_SPACE, "preserve")
