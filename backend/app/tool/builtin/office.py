"""Restricted declarative creation and editing for OOXML Office files.

This tool intentionally exposes a small data model instead of arbitrary Python
or shell execution.  Every output is written beside the destination, reopened
with the corresponding Office library, inspected for unsafe OOXML features,
and only then atomically installed.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import io
import logging
import math
import os
import re
import stat
import tempfile
import zipfile
from collections.abc import Iterable, Iterator, Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any
from xml.etree import ElementTree

from app.storage.file_versions import FileVersionStore
from app.tool.base import ToolDefinition, ToolResult
from app.tool.context import ToolContext
from app.tool.file_metadata import (
    UnsupportedFileMetadataError,
    ensure_mutation_metadata_supported,
)
from app.tool.file_versioning import version_metadata
from app.tool.workspace import WorkspaceViolation, resolve_and_validate, resolve_for_write
from app.tool.workspace_transaction import (
    WorkspaceMutationError,
    WorkspaceMutationTransaction,
)


logger = logging.getLogger(__name__)


_FORMATS = {
    ".docx": "document",
    ".xlsx": "workbook",
    ".pptx": "presentation",
}
_MIME_TYPES = {
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}


def _declared_local_image_paths(
    args: Mapping[str, Any],
    workspace: str,
) -> tuple[str, ...]:
    """Collect existing local image inputs for sparse transactional staging.

    This is intentionally a non-validating discovery pass.  The normal Office
    parsers below remain responsible for precise user-facing errors.
    """

    candidates: list[Any] = []
    document = args.get("document")
    if isinstance(document, Mapping):
        images = document.get("images")
        if isinstance(images, Sequence) and not isinstance(images, (str, bytes)):
            candidates.extend(images)
    presentation = args.get("presentation")
    if isinstance(presentation, Mapping):
        slides = presentation.get("slides")
        if isinstance(slides, Sequence) and not isinstance(slides, (str, bytes)):
            for slide in slides:
                if not isinstance(slide, Mapping):
                    continue
                images = slide.get("images")
                if isinstance(images, Sequence) and not isinstance(images, (str, bytes)):
                    candidates.extend(images)

    resolved: set[str] = set()
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            continue
        raw_path = candidate.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip() or "://" in raw_path:
            continue
        try:
            path = resolve_and_validate(raw_path, workspace)
        except WorkspaceViolation:
            continue
        if Path(path).is_file():
            resolved.add(path)
    return tuple(sorted(resolved))
_REQUIRED_PARTS = {
    ".docx": "word/document.xml",
    ".xlsx": "xl/workbook.xml",
    ".pptx": "ppt/presentation.xml",
}
_MACRO_OR_TEMPLATE_EXTENSIONS = {
    ".docm",
    ".dotm",
    ".dotx",
    ".xls",
    ".xlsb",
    ".xlsm",
    ".xltm",
    ".xltx",
    ".potm",
    ".potx",
    ".ppam",
    ".pps",
    ".ppsm",
    ".ppsx",
    ".ppt",
    ".pptm",
}
_UNSUPPORTED_RELATIONSHIP_KINDS = frozenset(
    {
        "activexcontrol",
        "activexcontrolbinary",
        "attachedtemplate",
        "audio",
        "control",
        "controlprop",
        "ctrlprop",
        "embeddedobject",
        "embeddedpackage",
        "externallink",
        "media",
        "oleobject",
        "package",
        "vbaproject",
        "vbaprojectsignature",
        "vbaprojectsignatureagile",
        "vbaprojectsignaturev3",
        "video",
    }
)
_UNSUPPORTED_EMBEDDED_PATH_SEGMENTS = frozenset(
    {"activex", "controls", "ctrlprops", "embeddings"}
)
_UNSUPPORTED_EMBEDDED_CONTENT_TYPE_MARKERS = (
    b"controlproperties",
    b"ms-office.activex",
    b"officedocument.oleobject",
)
_CUSTOM_XML_RELATIONSHIP_TYPE = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/customXml"
)
_COMMON_EDIT_RELATIONSHIP_TYPES = frozenset(
    {
        "http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties",
        "http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument",
        "http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties",
        "http://schemas.openxmlformats.org/package/2006/relationships/metadata/thumbnail",
    }
)
_ALLOWED_EDIT_RELATIONSHIP_TYPES = {
    ".docx": _COMMON_EDIT_RELATIONSHIP_TYPES
    | frozenset(
        {
            "http://schemas.microsoft.com/office/2007/relationships/stylesWithEffects",
            "http://schemas.openxmlformats.org/officeDocument/2006/relationships/fontTable",
            "http://schemas.openxmlformats.org/officeDocument/2006/relationships/footer",
            "http://schemas.openxmlformats.org/officeDocument/2006/relationships/header",
            "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink",
            "http://schemas.openxmlformats.org/officeDocument/2006/relationships/image",
            "http://schemas.openxmlformats.org/officeDocument/2006/relationships/numbering",
            "http://schemas.openxmlformats.org/officeDocument/2006/relationships/settings",
            "http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles",
            "http://schemas.openxmlformats.org/officeDocument/2006/relationships/theme",
            "http://schemas.openxmlformats.org/officeDocument/2006/relationships/webSettings",
        }
    ),
    ".xlsx": _COMMON_EDIT_RELATIONSHIP_TYPES
    | frozenset(
        {
            "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink",
            "http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles",
            "http://schemas.openxmlformats.org/officeDocument/2006/relationships/theme",
            "http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet",
        }
    ),
    ".pptx": _COMMON_EDIT_RELATIONSHIP_TYPES
    | frozenset(
        {
            "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink",
            "http://schemas.openxmlformats.org/officeDocument/2006/relationships/image",
            "http://schemas.openxmlformats.org/officeDocument/2006/relationships/presProps",
            "http://schemas.openxmlformats.org/officeDocument/2006/relationships/printerSettings",
            "http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide",
            "http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout",
            "http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideMaster",
            "http://schemas.openxmlformats.org/officeDocument/2006/relationships/tableStyles",
            "http://schemas.openxmlformats.org/officeDocument/2006/relationships/theme",
            "http://schemas.openxmlformats.org/officeDocument/2006/relationships/viewProps",
        }
    ),
}
_COMMON_EDIT_PART_PATTERNS = (
    re.compile(r"\[Content_Types\]\.xml"),
    re.compile(r"_rels/\.rels"),
    re.compile(r"docProps/(?:app|core)\.xml"),
    re.compile(r"docProps/thumbnail\.(?:jpe?g|png)"),
)
_ALLOWED_EDIT_PART_PATTERNS = {
    ".docx": _COMMON_EDIT_PART_PATTERNS
    + (
        re.compile(r"word/document\.xml"),
        re.compile(r"word/_rels/document\.xml\.rels"),
        re.compile(
            r"word/(?:fontTable|numbering|settings|styles|stylesWithEffects|webSettings)\.xml"
        ),
        re.compile(r"word/theme/theme\d+\.xml"),
        re.compile(r"word/(?:header|footer)\d+\.xml"),
        re.compile(r"word/_rels/(?:header|footer)\d+\.xml\.rels"),
        re.compile(
            r"word/media/[^/]+\.(?:bmp|emf|gif|jpe?g|png|tiff?|wmf)",
            re.IGNORECASE,
        ),
    ),
    ".xlsx": _COMMON_EDIT_PART_PATTERNS
    + (
        re.compile(r"xl/workbook\.xml"),
        re.compile(r"xl/_rels/workbook\.xml\.rels"),
        re.compile(r"xl/styles\.xml"),
        re.compile(r"xl/theme/theme\d+\.xml"),
        re.compile(r"xl/worksheets/sheet\d+\.xml"),
        re.compile(r"xl/worksheets/_rels/sheet\d+\.xml\.rels"),
    ),
    ".pptx": _COMMON_EDIT_PART_PATTERNS
    + (
        re.compile(r"ppt/presentation\.xml"),
        re.compile(r"ppt/_rels/presentation\.xml\.rels"),
        re.compile(r"ppt/(?:presProps|tableStyles|viewProps)\.xml"),
        re.compile(r"ppt/printerSettings/printerSettings\d+\.bin"),
        re.compile(r"ppt/slideMasters/slideMaster\d+\.xml"),
        re.compile(r"ppt/slideMasters/_rels/slideMaster\d+\.xml\.rels"),
        re.compile(r"ppt/slideLayouts/slideLayout\d+\.xml"),
        re.compile(r"ppt/slideLayouts/_rels/slideLayout\d+\.xml\.rels"),
        re.compile(r"ppt/slides/slide\d+\.xml"),
        re.compile(r"ppt/slides/_rels/slide\d+\.xml\.rels"),
        re.compile(r"ppt/theme/theme\d+\.xml"),
        re.compile(
            r"ppt/media/[^/]+\.(?:bmp|emf|gif|jpe?g|png|tiff?|wmf)",
            re.IGNORECASE,
        ),
    ),
}
_XLSX_WORKSHEET_PART = re.compile(
    r"xl/worksheets/(?:_rels/)?sheet\d+\.xml(?:\.rels)?"
)
_ALLOWED_TOP_LEVEL_ARGS = {
    "file_path",
    "operation",
    "overwrite",
    "document",
    "workbook",
    "presentation",
    "replacements",
}
_DOCX_STYLE_NAMES = {
    "normal": "Normal",
    "title": "Title",
    "subtitle": "Subtitle",
    "heading1": "Heading 1",
    "heading2": "Heading 2",
    "heading3": "Heading 3",
    "bullet": "List Bullet",
    "numbered": "List Number",
}
_INVALID_SHEET_TITLE = re.compile(r"[\\/*?:\[\]]")
_EXTERNAL_FORMULA = re.compile(
    r"(?:https?://|file://|\\\\|\[[^\]]+\.(?:csv|xls|xlsb|xlsm|xlsx)\])",
    re.IGNORECASE,
)

MAX_INPUT_FILE_BYTES = 50 * 1024 * 1024
MAX_OUTPUT_FILE_BYTES = 75 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 5_000
MAX_ARCHIVE_MEMBER_BYTES = 100 * 1024 * 1024
MAX_ARCHIVE_TOTAL_BYTES = 250 * 1024 * 1024
MAX_RELATIONSHIP_BYTES = 2 * 1024 * 1024
MAX_TEXT_CHARS = 1_000_000
MAX_TOTAL_TEXT_CHARS = 5_000_000
MAX_DECLARATIVE_ITEMS = 500_000
MAX_PARAGRAPHS = 2_000
MAX_TABLES = 200
MAX_TABLE_CELLS = 100_000
MAX_TABLE_COLUMNS = 256
MAX_SHEETS = 100
MAX_WORKBOOK_CELLS = 200_000
MAX_SLIDES = 300
MAX_BULLETS_PER_SLIDE = 500
MAX_PPTX_TABLES_PER_SLIDE = 20
MAX_PPTX_TABLE_CELLS_PER_SLIDE = 10_000
MAX_PPTX_TABLE_COLUMNS = 50
MAX_TEXT_BOXES_PER_SLIDE = 100
MAX_REPLACEMENTS = 200
MAX_IMAGES_PER_FILE = 100
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_TOTAL_IMAGE_BYTES = 50 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000
_IMAGE_EXTENSIONS = {".bmp", ".gif", ".jpeg", ".jpg", ".png", ".tif", ".tiff"}
_HEX_COLOR = re.compile(r"^(?:[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")


class OfficeInputError(ValueError):
    """A safe, request-localizable Office tool error."""

    def __init__(self, zh: str, en: str):
        self.zh = zh
        self.en = en
        super().__init__(en)


class OfficeTool(ToolDefinition):
    """Create or make bounded edits to macro-free OOXML Office files."""

    @property
    def id(self) -> str:
        return "office"

    @property
    def description(self) -> str:
        return (
            "Safely create or make limited declarative edits to .docx, .xlsx, and "
            ".pptx files inside the selected workspace. This tool does not run "
            "Python or shell commands and does not accept external templates or "
            "macro-enabled formats. DOCX supports paragraphs, tables, page breaks, "
            "local images, append, and exact text replacement; XLSX supports sheets, "
            "rows, cell updates, basic styles, and sheet deletion; PPTX supports "
            "title/bullet slides, text boxes, tables, local images, append, and exact "
            "text replacement. "
            "XLSX formulas beginning with '=' are stored but are never recalculated "
            "by this tool. Outputs are reopened and validated before atomic install."
        )

    def parameters_schema(self) -> dict[str, Any]:
        scalar_schema: dict[str, Any] = {
            "oneOf": [
                {"type": "string"},
                {"type": "number"},
                {"type": "boolean"},
                {"type": "null"},
            ]
        }
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": (
                        "Destination path. Relative paths are written under the "
                        "workspace's suxiaoyou_written directory. Only .docx, .xlsx, "
                        "and .pptx are accepted."
                    ),
                },
                "operation": {
                    "type": "string",
                    "enum": ["create", "edit"],
                    "description": (
                        "Create a new file, or edit an existing macro-free OOXML file."
                    ),
                },
                "overwrite": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "For create only: explicitly allow replacing an existing file."
                    ),
                },
                "document": {
                    "type": "object",
                    "description": "DOCX content. Valid only for a .docx path.",
                    "properties": {
                        "title": {"type": "string"},
                        "paragraphs": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "text": {"type": "string"},
                                    "style": {
                                        "type": "string",
                                        "enum": list(_DOCX_STYLE_NAMES),
                                        "default": "normal",
                                    },
                                    "page_break_after": {
                                        "type": "boolean",
                                        "default": False,
                                    },
                                },
                                "required": ["text"],
                            },
                        },
                        "tables": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "headers": {
                                        "type": "array",
                                        "items": scalar_schema,
                                    },
                                    "rows": {
                                        "type": "array",
                                        "items": {
                                            "type": "array",
                                            "items": scalar_schema,
                                        },
                                    },
                                },
                                "required": ["rows"],
                            },
                        },
                        "images": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "path": {"type": "string"},
                                    "width_inches": {"type": "number"},
                                    "caption": {"type": "string"},
                                },
                                "required": ["path"],
                            },
                        },
                    },
                },
                "workbook": {
                    "type": "object",
                    "description": "XLSX content. Valid only for a .xlsx path.",
                    "properties": {
                        "sheets": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "name": {"type": "string"},
                                    "action": {
                                        "type": "string",
                                        "enum": ["create", "append"],
                                    },
                                    "rows": {
                                        "type": "array",
                                        "items": {
                                            "type": "array",
                                            "items": scalar_schema,
                                        },
                                    },
                                },
                                "required": ["name", "rows"],
                            },
                        },
                        "cells": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "sheet": {"type": "string"},
                                    "cell": {"type": "string"},
                                    "value": scalar_schema,
                                    "style": {
                                        "type": "object",
                                        "properties": {
                                            "number_format": {"type": "string"},
                                            "font": {
                                                "type": "object",
                                                "properties": {
                                                    "bold": {"type": "boolean"},
                                                    "italic": {"type": "boolean"},
                                                    "color": {"type": "string"},
                                                    "size": {"type": "number"},
                                                },
                                            },
                                            "fill": {
                                                "type": "object",
                                                "properties": {
                                                    "color": {"type": "string"},
                                                },
                                                "required": ["color"],
                                            },
                                        },
                                    },
                                },
                                "required": ["sheet", "cell"],
                            },
                        },
                        "delete_sheets": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "For edit only: exact worksheet names to delete.",
                        },
                    },
                },
                "presentation": {
                    "type": "object",
                    "description": "PPTX content. Valid only for a .pptx path.",
                    "properties": {
                        "slides": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "title": {"type": "string"},
                                    "subtitle": {"type": "string"},
                                    "bullets": {
                                        "type": "array",
                                        "items": {
                                            "oneOf": [
                                                {"type": "string"},
                                                {
                                                    "type": "object",
                                                    "properties": {
                                                        "text": {"type": "string"},
                                                        "level": {
                                                            "type": "integer",
                                                            "minimum": 0,
                                                            "maximum": 4,
                                                        },
                                                    },
                                                    "required": ["text"],
                                                },
                                            ]
                                        },
                                    },
                                    "text_boxes": {
                                        "type": "array",
                                        "items": {
                                            "type": "object",
                                            "properties": {
                                                "text": {"type": "string"},
                                                "left_inches": {"type": "number"},
                                                "top_inches": {"type": "number"},
                                                "width_inches": {"type": "number"},
                                                "height_inches": {"type": "number"},
                                                "font_size": {"type": "number"},
                                            },
                                            "required": [
                                                "text",
                                                "left_inches",
                                                "top_inches",
                                                "width_inches",
                                                "height_inches",
                                            ],
                                        },
                                    },
                                    "tables": {
                                        "type": "array",
                                        "items": {
                                            "type": "object",
                                            "properties": {
                                                "left_inches": {"type": "number"},
                                                "top_inches": {"type": "number"},
                                                "width_inches": {"type": "number"},
                                                "height_inches": {"type": "number"},
                                                "headers": {
                                                    "type": "array",
                                                    "items": scalar_schema,
                                                },
                                                "rows": {
                                                    "type": "array",
                                                    "items": {
                                                        "type": "array",
                                                        "items": scalar_schema,
                                                    },
                                                },
                                            },
                                            "required": [
                                                "left_inches",
                                                "top_inches",
                                                "width_inches",
                                                "height_inches",
                                                "rows",
                                            ],
                                        },
                                    },
                                    "images": {
                                        "type": "array",
                                        "items": {
                                            "type": "object",
                                            "properties": {
                                                "path": {"type": "string"},
                                                "left_inches": {"type": "number"},
                                                "top_inches": {"type": "number"},
                                                "width_inches": {"type": "number"},
                                                "height_inches": {"type": "number"},
                                            },
                                            "required": [
                                                "path",
                                                "left_inches",
                                                "top_inches",
                                            ],
                                        },
                                    },
                                },
                                "required": ["title"],
                            },
                        },
                    },
                },
                "replacements": {
                    "type": "array",
                    "description": (
                        "For DOCX/PPTX edit: exact body text replacements. Matches "
                        "cannot span paragraphs. By default a match must be unique."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "old_text": {"type": "string"},
                            "new_text": {"type": "string"},
                            "replace_all": {"type": "boolean", "default": False},
                        },
                        "required": ["old_text", "new_text"],
                    },
                },
            },
            "required": ["file_path", "operation"],
        }

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if not ctx.workspace:
            return ToolResult(
                error=ctx.tr(
                    "Office 工具需要先选择工作区。",
                    "The Office tool requires a selected workspace.",
                )
            )
        if ctx.is_aborted:
            return ToolResult(error=ctx.tr("操作已取消。", "Operation cancelled."))

        try:
            file_path = resolve_for_write(str(args.get("file_path", "")), ctx.workspace)
        except WorkspaceViolation:
            return ToolResult(
                error=ctx.tr(
                    "拒绝访问：Office 文件必须位于当前工作区内。",
                    "Access denied: Office files must stay inside the current workspace.",
                )
            )

        transaction: WorkspaceMutationTransaction | None = None
        try:
            ensure_mutation_metadata_supported(file_path)
            transaction = WorkspaceMutationTransaction(
                ctx.workspace,
                ctx,
                operation=f"office.{args.get('operation', 'unknown')}",
            )
            image_paths = _declared_local_image_paths(args, ctx.workspace)
            staged_workspace = await asyncio.to_thread(
                transaction.prepare_paths,
                [file_path],
                read_paths=image_paths,
            )
            staged_target = transaction.staged_path(file_path)
            summary = await asyncio.to_thread(
                _run_office_operation,
                staged_target,
                args,
                ctx,
                staged_workspace,
            )
            if ctx.is_aborted:
                transaction.abort()
                return ToolResult(error=ctx.tr("操作已取消。", "Operation cancelled."))
            ensure_mutation_metadata_supported(file_path)
            commit = await asyncio.to_thread(transaction.commit)
        except asyncio.CancelledError:
            if transaction is not None:
                transaction.abort()
            raise
        except OfficeInputError as exc:
            if transaction is not None:
                transaction.abort()
            return ToolResult(error=ctx.tr(exc.zh, exc.en))
        except (UnsupportedFileMetadataError, WorkspaceMutationError) as exc:
            if transaction is not None:
                transaction.abort()
            return ToolResult(error=str(exc))
        except PermissionError:
            if transaction is not None:
                transaction.abort()
            return ToolResult(
                error=ctx.tr(
                    f"没有权限写入：{file_path}",
                    f"Permission denied writing: {file_path}",
                )
            )
        except OSError:
            if transaction is not None:
                transaction.abort()
            logger.exception("Office file write failed for %s", file_path)
            return ToolResult(
                error=ctx.tr(
                    "Office 文件写入失败，原文件未更改。",
                    "Office file write failed; the original file was not changed.",
                )
            )
        except Exception:
            if transaction is not None:
                transaction.abort()
            logger.exception("Office processing failed for %s", file_path)
            return ToolResult(
                error=ctx.tr(
                    "Office 文件处理或重新打开校验失败，原文件未更改。",
                    (
                        "Office processing or reopen validation failed; "
                        "the original file was not changed."
                    ),
                )
            )

        previous_version = None
        if commit.previous_version_ids:
            try:
                version_ids = set(commit.previous_version_ids)
                previous_version = next(
                    (
                        version
                        for version in FileVersionStore(Path(ctx.workspace)).list_versions()
                        if version.id in version_ids
                    ),
                    None,
                )
            except Exception:
                # The workspace commit is already durable.  A read-only metadata
                # lookup failure must not turn a completed write into a false
                # failure response or attempt an impossible post-commit rollback.
                logger.exception("Could not load Office version metadata for %s", file_path)
        summary = {**summary, **version_metadata(previous_version)}

        operation = str(args["operation"])
        action_zh = "已创建" if operation == "create" else "已编辑"
        action_en = "Created" if operation == "create" else "Edited"
        name = Path(file_path).name
        output = ctx.tr(
            f"{action_zh}并校验 {file_path}。文件已重新打开校验并原子替换。",
            (
                f"{action_en} and validated {file_path}. "
                "The file was reopened, verified, and atomically installed."
            ),
        )
        if summary["format"] == "xlsx":
            output += ctx.tr(
                " 公式会保存，但本工具不会重算公式结果。",
                " Formulas are stored, but this tool does not recalculate results.",
            )

        metadata = {
            "file_path": file_path,
            "mime_type": _MIME_TYPES[Path(file_path).suffix.lower()],
            "operation": operation,
            "format": summary["format"],
            "reopened_and_validated": True,
            "atomic_install": True,
            "macros_allowed": False,
            "external_templates_allowed": False,
            **commit.metadata,
            **summary,
        }
        if summary["format"] == "xlsx":
            metadata["formulas_recalculated"] = False

        return ToolResult(
            output=output,
            title=ctx.tr(f"{action_zh} {name}", f"{action_en} {name}"),
            metadata=metadata,
        )


def _run_office_operation(
    target: Path,
    args: Mapping[str, Any],
    ctx: ToolContext,
    staged_workspace: Path,
) -> dict[str, Any]:
    """Build and validate one Office file entirely in private staging."""

    unknown = sorted(set(args) - _ALLOWED_TOP_LEVEL_ARGS)
    if "template_path" in args or "template" in args:
        raise OfficeInputError(
            "Office 工具不接受外部模板。",
            "External templates are not accepted by the Office tool.",
        )
    if unknown:
        raise OfficeInputError(
            f"不支持的 Office 参数：{', '.join(unknown)}",
            f"Unsupported Office parameters: {', '.join(unknown)}",
        )
    _validate_request_budget(args)

    operation = args.get("operation")
    if operation not in {"create", "edit"}:
        raise OfficeInputError(
            "operation 必须是 create 或 edit。",
            "operation must be create or edit.",
        )
    overwrite = args.get("overwrite", False)
    if not isinstance(overwrite, bool):
        raise OfficeInputError("overwrite 必须是布尔值。", "overwrite must be a boolean.")
    if operation == "edit" and overwrite:
        raise OfficeInputError(
            "edit 操作不使用 overwrite。",
            "overwrite is not used with the edit operation.",
        )

    suffix = target.suffix.lower()
    if suffix in _MACRO_OR_TEMPLATE_EXTENSIONS:
        raise OfficeInputError(
            "不支持宏、模板或旧版 Office 格式；仅允许 .docx、.xlsx 和 .pptx。",
            (
                "Macro-enabled, template, and legacy Office formats are not supported; "
                "only .docx, .xlsx, and .pptx are allowed."
            ),
        )
    if suffix not in _FORMATS:
        raise OfficeInputError(
            "Office 文件扩展名必须是 .docx、.xlsx 或 .pptx。",
            "Office file extension must be .docx, .xlsx, or .pptx.",
        )

    expected_payload = _FORMATS[suffix]
    present_payloads = [name for name in _FORMATS.values() if args.get(name) is not None]
    replacements_only_edit = (
        operation == "edit"
        and suffix in {".docx", ".pptx"}
        and args.get("replacements") is not None
        and not present_payloads
    )
    if present_payloads != [expected_payload] and not replacements_only_edit:
        raise OfficeInputError(
            f"{suffix} 必须且只能提供 {expected_payload} 内容。",
            f"{suffix} requires exactly one {expected_payload} payload.",
        )
    if suffix == ".xlsx" and args.get("replacements") is not None:
        raise OfficeInputError(
            "XLSX 请使用 workbook.cells 或 workbook.sheets 进行编辑。",
            "Edit XLSX through workbook.cells or workbook.sheets.",
        )
    if operation == "create" and args.get("replacements") is not None:
        raise OfficeInputError(
            "replacements 仅用于编辑现有 DOCX/PPTX。",
            "replacements is only for editing an existing DOCX/PPTX.",
        )

    target.parent.mkdir(parents=True, exist_ok=True)
    target_exists = target.exists()
    target_mode = (
        stat.S_IMODE(target.stat().st_mode)
        if target_exists and target.is_file()
        else None
    )
    if operation == "edit":
        if not target_exists or not target.is_file():
            raise OfficeInputError(
                f"找不到要编辑的文件：{target}",
                f"File to edit was not found: {target}",
            )
        if target.stat().st_size > MAX_INPUT_FILE_BYTES:
            raise OfficeInputError(
                "Office 输入文件超过 50 MiB 限制。",
                "Office input exceeds the 50 MiB limit.",
            )
        source_parts = _inspect_ooxml_archive(target, suffix, audit_for_edit=True)
    else:
        source_parts = None
    if operation != "edit" and target_exists and not overwrite:
        raise OfficeInputError(
            f"文件已存在：{target}。如需替换，请显式设置 overwrite=true。",
            f"File already exists: {target}. Set overwrite=true to replace it explicitly.",
        )
    elif operation != "edit" and target.exists() and not target.is_file():
        raise OfficeInputError(
            f"目标不是普通文件：{target}",
            f"Target is not a regular file: {target}",
        )

    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=suffix,
        dir=target.parent,
    )
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        logical_workspace = Path(ctx.workspace or "").resolve()
        if suffix == ".docx":
            summary, expected = _write_docx(
                temporary,
                target,
                args,
                operation,
                logical_workspace,
                staged_workspace,
            )
        elif suffix == ".xlsx":
            summary, expected = _write_xlsx(temporary, target, args, operation)
        else:
            summary, expected = _write_pptx(
                temporary,
                target,
                args,
                operation,
                logical_workspace,
                staged_workspace,
            )

        _flush_file(temporary)
        if temporary.stat().st_size > MAX_OUTPUT_FILE_BYTES:
            raise OfficeInputError(
                "Office 输出文件超过 75 MiB 限制。",
                "Office output exceeds the 75 MiB limit.",
            )
        output_parts = _inspect_ooxml_archive(temporary, suffix)
        _reopen_and_verify(temporary, suffix, expected)
        if source_parts is not None:
            _verify_edit_part_preservation(
                source_parts,
                output_parts,
                suffix,
                args,
            )

        if target_mode is not None:
            try:
                os.chmod(temporary, target_mode)
            except OSError:
                pass
        _atomic_replace(temporary, target)
        _fsync_directory(target.parent)
        return summary
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _write_docx(
    temporary: Path,
    target: Path,
    args: Mapping[str, Any],
    operation: str,
    workspace: Path,
    staged_workspace: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    from docx import Document

    document = Document(str(target)) if operation == "edit" else Document()
    if operation == "create":
        _drop_default_docx_custom_xml(document)
    raw_payload = args.get("document")
    payload = (
        {}
        if operation == "edit" and raw_payload is None
        else _mapping(raw_payload, "document")
    )
    replacements = _parse_replacements(args.get("replacements"))
    paragraphs = _parse_docx_paragraphs(payload.get("paragraphs"))
    tables = _parse_tables(payload.get("tables"))
    images = _parse_docx_images(
        payload.get("images"),
        workspace,
        staged_workspace,
    )
    title = _optional_text(payload.get("title"), "document.title")

    if operation == "create" and not (title or paragraphs or tables or images):
        raise OfficeInputError(
            "DOCX 创建至少需要 title、paragraphs 或 tables 之一。",
            "DOCX creation requires title, paragraphs, or tables.",
        )
    if operation == "edit" and not (
        replacements or title or paragraphs or tables or images
    ):
        raise OfficeInputError(
            "DOCX 编辑至少需要一项变更。",
            "DOCX editing requires at least one change.",
        )

    replaced = _apply_replacements(
        list(_iter_docx_paragraphs(document)), replacements, "DOCX"
    )
    if title:
        document.core_properties.title = title
        document.add_heading(title, level=0)
    for item in paragraphs:
        paragraph = document.add_paragraph()
        paragraph.style = _DOCX_STYLE_NAMES[item["style"]]
        paragraph.add_run(item["text"])
        if item["page_break_after"]:
            document.add_page_break()
    for table_data in tables:
        headers = table_data["headers"]
        rows = table_data["rows"]
        column_count = max([len(headers), *(len(row) for row in rows)])
        table = document.add_table(rows=1 if headers else 0, cols=column_count)
        table.style = "Table Grid"
        if headers:
            for index, value in enumerate(headers):
                table.rows[0].cells[index].text = _cell_text(value)
        for row in rows:
            cells = table.add_row().cells
            for index, value in enumerate(row):
                cells[index].text = _cell_text(value)

    from docx.shared import Inches

    for image in images:
        width = Inches(image["width_inches"]) if image["width_inches"] else None
        document.add_picture(io.BytesIO(image["data"]), width=width)
        if image["caption"]:
            caption = document.add_paragraph(image["caption"])
            try:
                caption.style = "Caption"
            except KeyError:
                # Default python-docx templates include Caption.  A safe edit
                # of an unusual file should still retain the requested text.
                pass

    expected = {
        "semantic_digest": _semantic_digest(_docx_semantic(document)),
        "inline_shapes": len(document.inline_shapes),
        "page_breaks": _docx_page_break_count(document),
    }
    document.save(str(temporary))
    return (
        {
            "format": "docx",
            "paragraphs_added": len(paragraphs) + (1 if title else 0),
            "tables_added": len(tables),
            "images_added": len(images),
            "page_breaks_added": sum(
                int(item["page_break_after"]) for item in paragraphs
            ),
            "replacements": replaced,
        },
        expected,
    )


def _write_xlsx(
    temporary: Path,
    target: Path,
    args: Mapping[str, Any],
    operation: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    from openpyxl import Workbook, load_workbook

    if operation == "edit":
        workbook = load_workbook(
            str(target),
            read_only=False,
            data_only=False,
            keep_vba=False,
            keep_links=True,
        )
    else:
        workbook = Workbook()
        workbook.remove(workbook.active)

    try:
        return _write_xlsx_workbook(temporary, workbook, args, operation)
    finally:
        workbook.close()


def _write_xlsx_workbook(
    temporary: Path,
    workbook: Any,
    args: Mapping[str, Any],
    operation: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    from openpyxl.utils import get_column_letter
    from openpyxl.utils.cell import coordinate_to_tuple

    payload = _mapping(args.get("workbook"), "workbook")
    sheets = _sequence(payload.get("sheets", []), "workbook.sheets")
    cells = _sequence(payload.get("cells", []), "workbook.cells")
    delete_sheets = _sequence(
        payload.get("delete_sheets", []), "workbook.delete_sheets"
    )
    if operation == "create" and not sheets:
        raise OfficeInputError(
            "XLSX 创建至少需要一个 workbook.sheets 项。",
            "XLSX creation requires at least one workbook.sheets item.",
        )
    if operation == "create" and delete_sheets:
        raise OfficeInputError(
            "workbook.delete_sheets 仅用于编辑现有 XLSX。",
            "workbook.delete_sheets is only for editing an existing XLSX file.",
        )
    if operation == "edit" and not (sheets or cells or delete_sheets):
        raise OfficeInputError(
            "XLSX 编辑至少需要一项变更。",
            "XLSX editing requires at least one change.",
        )
    if len(sheets) > MAX_SHEETS:
        raise OfficeInputError(
            f"单次最多处理 {MAX_SHEETS} 个工作表。",
            f"At most {MAX_SHEETS} sheets may be processed in one call.",
        )

    written_cells: dict[tuple[str, str], Any] = {}
    written_styles: dict[tuple[str, str], dict[str, Any]] = {}
    formula_count = 0
    cell_count = 0
    created_sheets = 0
    deleted_sheets = 0
    appended_rows = 0
    styles_applied = 0
    requested_names: set[str] = set()

    delete_names: list[str] = []
    for index, raw_name in enumerate(delete_sheets):
        name = _required_text(raw_name, f"workbook.delete_sheets[{index}]")
        if name in delete_names:
            raise OfficeInputError(
                f"重复的工作表删除项：{name}",
                f"Duplicate sheet deletion: {name}",
            )
        if name not in workbook.sheetnames:
            raise OfficeInputError(
                f"找不到要删除的工作表：{name}",
                f"Sheet to delete was not found: {name}",
            )
        delete_names.append(name)
    if delete_names and len(delete_names) >= len(workbook.sheetnames):
        raise OfficeInputError(
            "XLSX 必须至少保留一个工作表。",
            "An XLSX file must retain at least one sheet.",
        )
    for name in delete_names:
        workbook.remove(workbook[name])
        deleted_sheets += 1

    for index, raw_sheet in enumerate(sheets):
        sheet = _mapping(raw_sheet, f"workbook.sheets[{index}]")
        name = _required_text(sheet.get("name"), f"workbook.sheets[{index}].name")
        _validate_sheet_name(name)
        folded = name.casefold()
        if folded in requested_names:
            raise OfficeInputError(
                f"工作表名重复：{name}",
                f"Duplicate sheet name: {name}",
            )
        requested_names.add(folded)

        action = sheet.get("action", "create" if operation == "create" else "append")
        if action not in {"create", "append"}:
            raise OfficeInputError(
                "workbook.sheets[].action 必须是 create 或 append。",
                "workbook.sheets[].action must be create or append.",
            )
        if operation == "create" and action != "create":
            raise OfficeInputError(
                "创建 XLSX 时，sheet action 只能是 create。",
                "Sheet action must be create when creating an XLSX file.",
            )
        if action == "create":
            if name.casefold() in {existing.casefold() for existing in workbook.sheetnames}:
                raise OfficeInputError(
                    f"工作表已存在：{name}",
                    f"Sheet already exists: {name}",
                )
            worksheet = workbook.create_sheet(name)
            created_sheets += 1
        else:
            if name not in workbook.sheetnames:
                raise OfficeInputError(
                    f"找不到工作表：{name}",
                    f"Sheet not found: {name}",
                )
            worksheet = workbook[name]

        rows = _sequence(sheet.get("rows"), f"workbook.sheets[{index}].rows")
        for row_index, raw_row in enumerate(rows):
            row = _row_values(raw_row, f"workbook.sheets[{index}].rows[{row_index}]")
            cell_count += len(row)
            if cell_count > MAX_WORKBOOK_CELLS:
                raise OfficeInputError(
                    f"单次最多写入 {MAX_WORKBOOK_CELLS} 个单元格。",
                    f"At most {MAX_WORKBOOK_CELLS} cells may be written in one call.",
                )
            for value in row:
                formula_count += int(_is_formula(value))
            worksheet.append(row)
            appended_rows += 1
            installed_row = worksheet.max_row
            for column_index, value in enumerate(row, 1):
                coordinate = f"{get_column_letter(column_index)}{installed_row}"
                written_cells[(name, coordinate)] = value

    for index, raw_cell in enumerate(cells):
        item = _mapping(raw_cell, f"workbook.cells[{index}]")
        sheet_name = _required_text(item.get("sheet"), f"workbook.cells[{index}].sheet")
        coordinate = _required_text(item.get("cell"), f"workbook.cells[{index}].cell").upper()
        if sheet_name not in workbook.sheetnames:
            raise OfficeInputError(
                f"找不到工作表：{sheet_name}",
                f"Sheet not found: {sheet_name}",
            )
        try:
            row_number, column_number = coordinate_to_tuple(coordinate)
        except (TypeError, ValueError):
            raise OfficeInputError(
                f"无效单元格坐标：{coordinate}",
                f"Invalid cell coordinate: {coordinate}",
            ) from None
        if not 1 <= row_number <= 1_048_576 or not 1 <= column_number <= 16_384:
            raise OfficeInputError(
                f"单元格坐标超出 XLSX 范围：{coordinate}",
                f"Cell coordinate exceeds XLSX limits: {coordinate}",
            )
        has_value = "value" in item
        has_style = item.get("style") is not None
        if not has_value and not has_style:
            raise OfficeInputError(
                f"workbook.cells[{index}] 至少需要 value 或 style。",
                f"workbook.cells[{index}] requires value or style.",
            )
        cell = workbook[sheet_name][coordinate]
        if has_value:
            value = _scalar(item["value"], f"workbook.cells[{index}].value")
            formula_count += int(_is_formula(value))
            cell.value = value
            written_cells[(sheet_name, coordinate)] = value
        if has_style:
            _apply_xlsx_style(cell, item["style"], f"workbook.cells[{index}].style")
            written_styles[(sheet_name, coordinate)] = _xlsx_style_snapshot(cell)
            styles_applied += 1
        cell_count += 1
        if cell_count > MAX_WORKBOOK_CELLS:
            raise OfficeInputError(
                f"单次最多写入 {MAX_WORKBOOK_CELLS} 个单元格。",
                f"At most {MAX_WORKBOOK_CELLS} cells may be written in one call.",
            )

    if not any(sheet.sheet_state == "visible" for sheet in workbook.worksheets):
        raise OfficeInputError(
            "XLSX 必须至少保留一个可见工作表。",
            "An XLSX file must retain at least one visible sheet.",
        )
    workbook.save(str(temporary))
    expected = {
        "sheet_names": tuple(workbook.sheetnames),
        "written_cells": written_cells,
        "written_styles": written_styles,
    }
    return (
        {
            "format": "xlsx",
            "sheets_created": created_sheets,
            "sheets_deleted": deleted_sheets,
            "rows_appended": appended_rows,
            "cells_written": cell_count,
            "styles_applied": styles_applied,
            "formulas_written": formula_count,
        },
        expected,
    )


def _write_pptx(
    temporary: Path,
    target: Path,
    args: Mapping[str, Any],
    operation: str,
    workspace: Path,
    staged_workspace: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    from pptx import Presentation

    presentation = Presentation(str(target)) if operation == "edit" else Presentation()
    raw_payload = args.get("presentation")
    payload = (
        {}
        if operation == "edit" and raw_payload is None
        else _mapping(raw_payload, "presentation")
    )
    raw_slides = _sequence(payload.get("slides", []), "presentation.slides")
    replacements = _parse_replacements(args.get("replacements"))
    if len(raw_slides) > MAX_SLIDES:
        raise OfficeInputError(
            f"单次最多添加 {MAX_SLIDES} 张幻灯片。",
            f"At most {MAX_SLIDES} slides may be added in one call.",
        )
    if operation == "create" and not raw_slides:
        raise OfficeInputError(
            "PPTX 创建至少需要一张 presentation.slides。",
            "PPTX creation requires at least one presentation.slides item.",
        )
    if operation == "edit" and not (raw_slides or replacements):
        raise OfficeInputError(
            "PPTX 编辑至少需要一项变更。",
            "PPTX editing requires at least one change.",
        )

    replaced = _apply_replacements(
        list(_iter_pptx_paragraphs(presentation)), replacements, "PPTX"
    )
    text_boxes_added = 0
    tables_added = 0
    images_added = 0
    total_image_bytes = 0
    for index, raw_slide in enumerate(raw_slides):
        slide_data = _mapping(raw_slide, f"presentation.slides[{index}]")
        title = _required_text(slide_data.get("title"), f"presentation.slides[{index}].title")
        subtitle = _optional_text(
            slide_data.get("subtitle"), f"presentation.slides[{index}].subtitle"
        )
        bullets = _parse_bullets(
            slide_data.get("bullets", []), f"presentation.slides[{index}].bullets"
        )
        text_boxes = _parse_pptx_text_boxes(
            slide_data.get("text_boxes", []),
            f"presentation.slides[{index}].text_boxes",
        )
        tables = _parse_pptx_tables(
            slide_data.get("tables", []), f"presentation.slides[{index}].tables"
        )
        images = _parse_pptx_images(
            slide_data.get("images", []),
            workspace,
            staged_workspace,
            f"presentation.slides[{index}].images",
        )
        images_added += len(images)
        if images_added > MAX_IMAGES_PER_FILE:
            raise OfficeInputError(
                f"单个 Office 文件最多添加 {MAX_IMAGES_PER_FILE} 张图片。",
                f"At most {MAX_IMAGES_PER_FILE} images may be added to one Office file.",
            )
        total_image_bytes += sum(len(image["data"]) for image in images)
        _validate_total_image_bytes(total_image_bytes)
        if subtitle is not None and bullets:
            raise OfficeInputError(
                "单张幻灯片不能同时提供 subtitle 和 bullets。",
                "A slide cannot provide subtitle and bullets at the same time.",
            )
        _add_slide(
            presentation,
            title,
            subtitle,
            bullets,
            text_boxes,
            tables,
            images,
        )
        text_boxes_added += len(text_boxes)
        tables_added += len(tables)

    expected = {
        "semantic_digest": _semantic_digest(_pptx_semantic(presentation)),
        "shape_counts": _pptx_shape_counts(presentation),
    }
    presentation.save(str(temporary))
    return (
        {
            "format": "pptx",
            "slides_added": len(raw_slides),
            "text_boxes_added": text_boxes_added,
            "tables_added": tables_added,
            "images_added": images_added,
            "replacements": replaced,
        },
        expected,
    )


def _reopen_and_verify(path: Path, suffix: str, expected: Mapping[str, Any]) -> None:
    """Reopen with the format library and compare a declarative semantic model."""

    try:
        if suffix == ".docx":
            from docx import Document

            reopened = Document(str(path))
            actual = _semantic_digest(_docx_semantic(reopened))
            if actual != expected["semantic_digest"]:
                raise ValueError("DOCX semantic verification mismatch")
            if len(reopened.inline_shapes) != expected["inline_shapes"]:
                raise ValueError("DOCX image verification mismatch")
            if _docx_page_break_count(reopened) != expected["page_breaks"]:
                raise ValueError("DOCX page-break verification mismatch")
        elif suffix == ".pptx":
            from pptx import Presentation

            reopened = Presentation(str(path))
            actual = _semantic_digest(_pptx_semantic(reopened))
            if actual != expected["semantic_digest"]:
                raise ValueError("PPTX semantic verification mismatch")
            if _pptx_shape_counts(reopened) != expected["shape_counts"]:
                raise ValueError("PPTX shape verification mismatch")
        else:
            from openpyxl import load_workbook

            workbook = load_workbook(
                str(path),
                read_only=False,
                data_only=False,
                keep_vba=False,
                keep_links=False,
            )
            try:
                if tuple(workbook.sheetnames) != expected["sheet_names"]:
                    raise ValueError("XLSX sheet verification mismatch")
                for (sheet_name, coordinate), value in expected["written_cells"].items():
                    if workbook[sheet_name][coordinate].value != value:
                        raise ValueError("XLSX cell verification mismatch")
                for (sheet_name, coordinate), style in expected["written_styles"].items():
                    if _xlsx_style_snapshot(workbook[sheet_name][coordinate]) != style:
                        raise ValueError("XLSX style verification mismatch")
            finally:
                workbook.close()
    except OfficeInputError:
        raise
    except Exception as exc:
        raise OfficeInputError(
            "Office 文件重新打开校验失败，未替换原文件。",
            "Office reopen validation failed; the original file was not replaced.",
        ) from exc


def _drop_default_docx_custom_xml(document: Any) -> None:
    """Remove python-docx's bundled bibliography customXml from new files.

    Existing custom XML is rejected during the edit compatibility audit.  New
    documents must therefore not inherit the library template's unmanaged
    customXml part or they would become uneditable on their next operation.
    """

    for relationship_id, relationship in list(document.part.rels.items()):
        if relationship.reltype.rstrip("/") == _CUSTOM_XML_RELATIONSHIP_TYPE:
            document.part.drop_rel(relationship_id)


def _edit_part_is_allowed(name: str, suffix: str) -> bool:
    return any(
        pattern.fullmatch(name)
        for pattern in _ALLOWED_EDIT_PART_PATTERNS[suffix]
    )


def _inspect_ooxml_archive(
    path: Path,
    suffix: str,
    *,
    audit_for_edit: bool = False,
) -> frozenset[str]:
    """Reject malformed, unsafe, lossy, or oversized OOXML inputs.

    Editing uses a deliberately small package-part and relationship allowlist.
    The format libraries are permitted to touch only structures whose
    round-trip behavior is covered by the Office contract; unfamiliar OOXML is
    rejected before it can be silently removed during save.
    """

    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            if len(infos) > MAX_ARCHIVE_ENTRIES:
                raise OfficeInputError(
                    "Office 压缩包条目过多。",
                    "Office archive contains too many entries.",
                )
            names: set[str] = set()
            folded_names: set[str] = set()
            total_bytes = 0
            for info in infos:
                name = info.filename
                pure = PurePosixPath(name)
                if (
                    not name
                    or name.startswith("/")
                    or "\\" in name
                    or ".." in pure.parts
                    or pure.is_absolute()
                ):
                    raise OfficeInputError(
                        "Office 压缩包包含不安全路径。",
                        "Office archive contains an unsafe path.",
                    )
                folded = name.casefold()
                if name in names or folded in folded_names:
                    raise OfficeInputError(
                        "Office 压缩包包含重复条目。",
                        "Office archive contains duplicate entries.",
                    )
                names.add(name)
                folded_names.add(folded)
                folded_parts = {part.casefold() for part in pure.parts}
                if audit_for_edit and "customxml" in folded_parts:
                    raise OfficeInputError(
                        "Office 文件包含本工具无法保证保真的 customXml 数据，已拒绝编辑。",
                        (
                            "Office file contains customXml data that this tool cannot "
                            "guarantee to preserve; editing was refused."
                        ),
                    )
                if folded_parts & _UNSUPPORTED_EMBEDDED_PATH_SEGMENTS:
                    raise OfficeInputError(
                        "Office 文件包含本工具无法安全保留的嵌入对象或控件。",
                        (
                            "Office file contains an embedded object or control that "
                            "this tool cannot safely preserve."
                        ),
                    )
                if info.flag_bits & 0x1:
                    raise OfficeInputError(
                        "不支持加密的 Office 文件。",
                        "Encrypted Office files are not supported.",
                    )
                if info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
                    raise OfficeInputError(
                        "Office 压缩包使用了不支持的压缩方式。",
                        "Office archive uses an unsupported compression method.",
                    )
                if info.file_size > MAX_ARCHIVE_MEMBER_BYTES:
                    raise OfficeInputError(
                        "Office 压缩包中的单个文件过大。",
                        "An Office archive member is too large.",
                    )
                total_bytes += info.file_size
                if total_bytes > MAX_ARCHIVE_TOTAL_BYTES:
                    raise OfficeInputError(
                        "Office 压缩包解压后过大。",
                        "Office archive is too large after decompression.",
                    )
                if info.file_size and info.compress_size == 0:
                    raise OfficeInputError(
                        "Office 压缩包的压缩比异常。",
                        "Office archive has a suspicious compression ratio.",
                    )
                if info.compress_size and info.file_size / info.compress_size > 500:
                    raise OfficeInputError(
                        "Office 压缩包的压缩比过高。",
                        "Office archive compression ratio is too high.",
                    )

            if "[Content_Types].xml" not in names or _REQUIRED_PARTS[suffix] not in names:
                raise OfficeInputError(
                    f"文件不是有效的 {suffix} OOXML 文档。",
                    f"File is not a valid {suffix} OOXML document.",
                )
            if any(name.casefold().endswith("vbaproject.bin") for name in names):
                raise OfficeInputError(
                    "检测到 Office 宏；本工具不处理宏。",
                    "Office macros were detected; this tool does not process macros.",
                )

            content_types_info = archive.getinfo("[Content_Types].xml")
            if content_types_info.file_size > MAX_RELATIONSHIP_BYTES:
                raise OfficeInputError(
                    "Office 内容类型清单过大。",
                    "The Office content-types manifest is too large.",
                )
            content_types = archive.read(content_types_info)
            lowered_types = content_types.lower()
            if (
                b"macroenabled" in lowered_types
                or b"vbaproject" in lowered_types
                or b"wordprocessingml.template" in lowered_types
                or b"spreadsheetml.template" in lowered_types
                or b"presentationml.template" in lowered_types
            ):
                raise OfficeInputError(
                    "检测到宏或 Office 模板内容类型。",
                    "A macro or Office template content type was detected.",
                )
            if any(
                marker in lowered_types
                for marker in _UNSUPPORTED_EMBEDDED_CONTENT_TYPE_MARKERS
            ):
                raise OfficeInputError(
                    "Office 内容类型清单中包含不支持的嵌入对象或控件。",
                    (
                        "The Office content-types manifest contains an unsupported "
                        "embedded object or control."
                    ),
                )

            for info in infos:
                if not info.filename.casefold().endswith(".rels"):
                    continue
                if info.file_size > MAX_RELATIONSHIP_BYTES:
                    raise OfficeInputError(
                        "Office 关系文件过大。",
                        "An Office relationships file is too large.",
                    )
                try:
                    root = ElementTree.fromstring(archive.read(info))
                except ElementTree.ParseError as exc:
                    raise OfficeInputError(
                        "Office 关系文件无法解析。",
                        "An Office relationships file could not be parsed.",
                    ) from exc
                for relationship in root.iter():
                    if not relationship.tag.endswith("Relationship"):
                        continue
                    rel_type = relationship.attrib.get("Type", "").strip()
                    rel_kind = rel_type.rstrip("/").rsplit("/", 1)[-1].casefold()
                    if rel_kind in _UNSUPPORTED_RELATIONSHIP_KINDS:
                        raise OfficeInputError(
                            "Office 文件包含外部模板、外部工作簿或嵌入对象。",
                            (
                                "Office file contains an external template, external "
                                "workbook, macro, or embedded object."
                            ),
                        )
                    if (
                        audit_for_edit
                        and rel_type.rstrip("/")
                        not in _ALLOWED_EDIT_RELATIONSHIP_TYPES[suffix]
                    ):
                        raise OfficeInputError(
                            "Office 文件包含本工具无法保证保真的关系类型，已拒绝编辑。",
                            (
                                "Office file contains an unsupported relationship type "
                                "that this tool cannot guarantee to preserve; editing "
                                "was refused."
                            ),
                        )

            if audit_for_edit:
                unsupported_parts = sorted(
                    name for name in names if not _edit_part_is_allowed(name, suffix)
                )
                if unsupported_parts:
                    preview = ", ".join(unsupported_parts[:3])
                    raise OfficeInputError(
                        "Office 文件包含本工具无法保证保真的部件，已拒绝编辑。",
                        (
                            "Office file contains a package part that this tool cannot "
                            f"guarantee to preserve ({preview}); editing was refused."
                        ),
                    )
    except OfficeInputError:
        raise
    except (OSError, zipfile.BadZipFile) as exc:
        raise OfficeInputError(
            f"无法读取 {suffix} OOXML 文档。",
            f"Could not read the {suffix} OOXML document.",
        ) from exc
    return frozenset(names)


def _verify_edit_part_preservation(
    source_parts: frozenset[str],
    output_parts: frozenset[str],
    suffix: str,
    args: Mapping[str, Any],
) -> None:
    """Fail before installation if a supported input part disappeared."""

    missing = set(source_parts - output_parts)
    if suffix == ".xlsx":
        workbook = args.get("workbook")
        deleting_sheets = isinstance(workbook, Mapping) and bool(
            workbook.get("delete_sheets")
        )
        if deleting_sheets:
            # openpyxl may renumber worksheet part names after an explicit sheet
            # deletion.  Sheet names and requested values are verified by the
            # semantic reopen check, while every non-worksheet part remains
            # subject to exact preservation.
            missing = {
                name for name in missing if not _XLSX_WORKSHEET_PART.fullmatch(name)
            }
    if missing:
        preview = ", ".join(sorted(missing)[:3])
        raise OfficeInputError(
            "Office 编辑会删除原文件中的部件，已取消并保留原文件。",
            (
                "Office edit would remove existing package parts "
                f"({preview}); the edit was cancelled and the original was preserved."
            ),
        )


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise OfficeInputError(f"{name} 必须是对象。", f"{name} must be an object.")
    return value


def _validate_request_budget(value: Mapping[str, Any]) -> None:
    """Bound aggregate declarative input before any directory or temp creation."""

    stack: list[Any] = list(value.values())
    visited: set[int] = set()
    text_chars = 0
    items = 0
    while stack:
        current = stack.pop()
        items += 1
        if items > MAX_DECLARATIVE_ITEMS:
            raise OfficeInputError(
                "Office 请求包含的声明式项目过多。",
                "The Office request contains too many declarative items.",
            )
        if isinstance(current, str):
            text_chars += len(current)
            if text_chars > MAX_TOTAL_TEXT_CHARS:
                raise OfficeInputError(
                    f"Office 请求的文本总量超过 {MAX_TOTAL_TEXT_CHARS} 个字符。",
                    (
                        "The Office request exceeds the aggregate "
                        f"{MAX_TOTAL_TEXT_CHARS}-character limit."
                    ),
                )
            continue
        if isinstance(current, Mapping):
            identity = id(current)
            if identity in visited:
                continue
            visited.add(identity)
            stack.extend(current.values())
        elif isinstance(current, Sequence) and not isinstance(current, (bytes, bytearray)):
            identity = id(current)
            if identity in visited:
                continue
            visited.add(identity)
            stack.extend(current)


def _sequence(value: Any, name: str) -> Sequence[Any]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise OfficeInputError(f"{name} 必须是数组。", f"{name} must be an array.")
    return value


def _required_text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise OfficeInputError(
            f"{name} 必须是非空字符串。",
            f"{name} must be a non-empty string.",
        )
    return _bounded_text(value, name)


def _optional_text(value: Any, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise OfficeInputError(f"{name} 必须是字符串。", f"{name} must be a string.")
    return _bounded_text(value, name)


def _bounded_text(value: str, name: str) -> str:
    if len(value) > MAX_TEXT_CHARS:
        raise OfficeInputError(
            f"{name} 超过 {MAX_TEXT_CHARS} 个字符的限制。",
            f"{name} exceeds the {MAX_TEXT_CHARS}-character limit.",
        )
    if "\x00" in value:
        raise OfficeInputError(
            f"{name} 包含不允许的空字符。",
            f"{name} contains a forbidden null character.",
        )
    return value


def _scalar(value: Any, name: str) -> str | int | float | bool | None:
    if value is None or isinstance(value, (str, bool, int, float)):
        if isinstance(value, str):
            _bounded_text(value, name)
            if _is_formula(value) and _EXTERNAL_FORMULA.search(value):
                raise OfficeInputError(
                    f"{name} 公式包含外部工作簿或网络引用。",
                    f"{name} formula contains an external workbook or network reference.",
                )
        if isinstance(value, float) and not math.isfinite(value):
            raise OfficeInputError(
                f"{name} 必须是有限数值。",
                f"{name} must be a finite number.",
            )
        return value
    raise OfficeInputError(
        f"{name} 必须是字符串、数字、布尔值或 null。",
        f"{name} must be a string, number, boolean, or null.",
    )


def _bounded_number(
    value: Any,
    name: str,
    *,
    minimum: float,
    maximum: float,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise OfficeInputError(f"{name} 必须是数字。", f"{name} must be a number.")
    number = float(value)
    if not math.isfinite(number) or not minimum <= number <= maximum:
        raise OfficeInputError(
            f"{name} 必须介于 {minimum} 和 {maximum} 之间。",
            f"{name} must be between {minimum} and {maximum}.",
        )
    return number


def _read_local_image(
    value: Any,
    workspace: Path,
    staged_workspace: Path,
    name: str,
) -> dict[str, Any]:
    from PIL import Image, UnidentifiedImageError

    raw_path = _required_text(value, name)
    if "://" in raw_path:
        raise OfficeInputError(
            f"{name} 只允许工作区内的本地图片。",
            f"{name} accepts only a local image inside the workspace.",
        )
    try:
        logical_path = Path(resolve_and_validate(raw_path, str(workspace)))
    except WorkspaceViolation as exc:
        raise OfficeInputError(
            f"{name} 必须位于当前工作区内。",
            f"{name} must stay inside the current workspace.",
        ) from exc
    try:
        relative = logical_path.relative_to(workspace)
    except ValueError as exc:  # pragma: no cover - guarded by resolve_and_validate
        raise OfficeInputError(
            f"{name} 必须位于当前工作区内。",
            f"{name} must stay inside the current workspace.",
        ) from exc
    resolved = staged_workspace / relative
    if not resolved.is_file():
        raise OfficeInputError(
            f"找不到本地图片：{raw_path}",
            f"Local image was not found: {raw_path}",
        )
    if resolved.suffix.lower() not in _IMAGE_EXTENSIONS:
        raise OfficeInputError(
            f"{name} 使用了不支持的图片格式。",
            f"{name} uses an unsupported image format.",
        )
    try:
        size = resolved.stat().st_size
        if size > MAX_IMAGE_BYTES:
            raise OfficeInputError(
                f"单张 Office 图片不能超过 {MAX_IMAGE_BYTES // (1024 * 1024)} MiB。",
                (
                    "An Office image cannot exceed "
                    f"{MAX_IMAGE_BYTES // (1024 * 1024)} MiB."
                ),
            )
        data = resolved.read_bytes()
        if len(data) != size:
            raise OfficeInputError(
                "读取期间本地图片发生了变化。",
                "The local image changed while it was being read.",
            )
        with Image.open(io.BytesIO(data)) as image:
            width, height = image.size
            if width <= 0 or height <= 0 or width * height > MAX_IMAGE_PIXELS:
                raise OfficeInputError(
                    "Office 图片像素数超过安全限制。",
                    "The Office image exceeds the safe pixel limit.",
                )
            image.verify()
    except OfficeInputError:
        raise
    except (OSError, UnidentifiedImageError) as exc:
        raise OfficeInputError(
            f"无法验证本地图片：{raw_path}",
            f"Could not validate the local image: {raw_path}",
        ) from exc
    return {"path": resolved, "data": data}


def _validate_total_image_bytes(total_bytes: int) -> None:
    if total_bytes > MAX_TOTAL_IMAGE_BYTES:
        raise OfficeInputError(
            f"单个 Office 请求的图片总量不能超过 {MAX_TOTAL_IMAGE_BYTES // (1024 * 1024)} MiB。",
            (
                "Images in one Office request cannot exceed "
                f"{MAX_TOTAL_IMAGE_BYTES // (1024 * 1024)} MiB in total."
            ),
        )


def _row_values(value: Any, name: str) -> list[str | int | float | bool | None]:
    row = _sequence(value, name)
    if not row:
        raise OfficeInputError(f"{name} 不能为空。", f"{name} cannot be empty.")
    if len(row) > 16_384:
        raise OfficeInputError(
            f"{name} 超过 XLSX 最大列数。",
            f"{name} exceeds the XLSX column limit.",
        )
    return [_scalar(item, f"{name}[{index}]") for index, item in enumerate(row)]


def _parse_docx_paragraphs(value: Any) -> list[dict[str, Any]]:
    paragraphs = _sequence([] if value is None else value, "document.paragraphs")
    if len(paragraphs) > MAX_PARAGRAPHS:
        raise OfficeInputError(
            f"单次最多添加 {MAX_PARAGRAPHS} 个段落。",
            f"At most {MAX_PARAGRAPHS} paragraphs may be added in one call.",
        )
    parsed: list[dict[str, Any]] = []
    for index, raw in enumerate(paragraphs):
        item = _mapping(raw, f"document.paragraphs[{index}]")
        text = _optional_text(item.get("text"), f"document.paragraphs[{index}].text")
        if text is None:
            raise OfficeInputError(
                f"document.paragraphs[{index}].text 不能缺失。",
                f"document.paragraphs[{index}].text is required.",
            )
        style_name = item.get("style", "normal")
        if style_name not in _DOCX_STYLE_NAMES:
            raise OfficeInputError(
                f"不支持的 DOCX 段落样式：{style_name}",
                f"Unsupported DOCX paragraph style: {style_name}",
            )
        page_break_after = item.get("page_break_after", False)
        if not isinstance(page_break_after, bool):
            raise OfficeInputError(
                f"document.paragraphs[{index}].page_break_after 必须是布尔值。",
                f"document.paragraphs[{index}].page_break_after must be a boolean.",
            )
        parsed.append(
            {
                "text": text,
                "style": str(style_name),
                "page_break_after": page_break_after,
            }
        )
    return parsed


def _parse_docx_images(
    value: Any,
    workspace: Path,
    staged_workspace: Path,
) -> list[dict[str, Any]]:
    images = _sequence([] if value is None else value, "document.images")
    if len(images) > MAX_IMAGES_PER_FILE:
        raise OfficeInputError(
            f"单个 Office 文件最多添加 {MAX_IMAGES_PER_FILE} 张图片。",
            f"At most {MAX_IMAGES_PER_FILE} images may be added to one Office file.",
        )
    parsed: list[dict[str, Any]] = []
    total_bytes = 0
    for index, raw in enumerate(images):
        item = _mapping(raw, f"document.images[{index}]")
        image = _read_local_image(
            item.get("path"),
            workspace,
            staged_workspace,
            f"document.images[{index}].path",
        )
        total_bytes += len(image["data"])
        _validate_total_image_bytes(total_bytes)
        width = item.get("width_inches")
        if width is not None:
            width = _bounded_number(
                width,
                f"document.images[{index}].width_inches",
                minimum=0.1,
                maximum=50,
            )
        caption = _optional_text(
            item.get("caption"), f"document.images[{index}].caption"
        )
        parsed.append({**image, "width_inches": width, "caption": caption})
    return parsed


def _parse_tables(value: Any) -> list[dict[str, list[Any]]]:
    tables = _sequence([] if value is None else value, "document.tables")
    if len(tables) > MAX_TABLES:
        raise OfficeInputError(
            f"单次最多添加 {MAX_TABLES} 个表格。",
            f"At most {MAX_TABLES} tables may be added in one call.",
        )
    parsed: list[dict[str, list[Any]]] = []
    total_cells = 0
    for index, raw in enumerate(tables):
        table = _mapping(raw, f"document.tables[{index}]")
        headers = [
            _scalar(item, f"document.tables[{index}].headers[{cell_index}]")
            for cell_index, item in enumerate(
                _sequence(table.get("headers", []), f"document.tables[{index}].headers")
            )
        ]
        raw_rows = _sequence(table.get("rows"), f"document.tables[{index}].rows")
        rows = [
            _row_values(row, f"document.tables[{index}].rows[{row_index}]")
            for row_index, row in enumerate(raw_rows)
        ]
        if not headers and not rows:
            raise OfficeInputError(
                f"document.tables[{index}] 不能为空。",
                f"document.tables[{index}] cannot be empty.",
            )
        widest_row = max([len(headers), *(len(row) for row in rows)])
        if widest_row > MAX_TABLE_COLUMNS:
            raise OfficeInputError(
                f"DOCX 表格最多支持 {MAX_TABLE_COLUMNS} 列。",
                f"DOCX tables support at most {MAX_TABLE_COLUMNS} columns.",
            )
        total_cells += len(headers) + sum(len(row) for row in rows)
        if total_cells > MAX_TABLE_CELLS:
            raise OfficeInputError(
                f"单次最多添加 {MAX_TABLE_CELLS} 个 DOCX 表格单元格。",
                f"At most {MAX_TABLE_CELLS} DOCX table cells may be added in one call.",
            )
        parsed.append({"headers": headers, "rows": rows})
    return parsed


def _parse_pptx_text_boxes(value: Any, name: str) -> list[dict[str, Any]]:
    items = _sequence(value, name)
    if len(items) > MAX_TEXT_BOXES_PER_SLIDE:
        raise OfficeInputError(
            f"单张幻灯片最多添加 {MAX_TEXT_BOXES_PER_SLIDE} 个文本框。",
            f"A slide supports at most {MAX_TEXT_BOXES_PER_SLIDE} text boxes.",
        )
    parsed: list[dict[str, Any]] = []
    for index, raw in enumerate(items):
        item_name = f"{name}[{index}]"
        item = _mapping(raw, item_name)
        text = _required_text(item.get("text"), f"{item_name}.text")
        parsed.append(
            {
                "text": text,
                "left_inches": _bounded_number(
                    item.get("left_inches"),
                    f"{item_name}.left_inches",
                    minimum=0,
                    maximum=100,
                ),
                "top_inches": _bounded_number(
                    item.get("top_inches"),
                    f"{item_name}.top_inches",
                    minimum=0,
                    maximum=100,
                ),
                "width_inches": _bounded_number(
                    item.get("width_inches"),
                    f"{item_name}.width_inches",
                    minimum=0.1,
                    maximum=100,
                ),
                "height_inches": _bounded_number(
                    item.get("height_inches"),
                    f"{item_name}.height_inches",
                    minimum=0.1,
                    maximum=100,
                ),
                "font_size": (
                    _bounded_number(
                        item["font_size"],
                        f"{item_name}.font_size",
                        minimum=1,
                        maximum=200,
                    )
                    if item.get("font_size") is not None
                    else None
                ),
            }
        )
    return parsed


def _parse_pptx_tables(value: Any, name: str) -> list[dict[str, Any]]:
    items = _sequence(value, name)
    if len(items) > MAX_PPTX_TABLES_PER_SLIDE:
        raise OfficeInputError(
            f"单张幻灯片最多添加 {MAX_PPTX_TABLES_PER_SLIDE} 个表格。",
            f"A slide supports at most {MAX_PPTX_TABLES_PER_SLIDE} tables.",
        )
    parsed: list[dict[str, Any]] = []
    total_cells = 0
    for index, raw in enumerate(items):
        item_name = f"{name}[{index}]"
        item = _mapping(raw, item_name)
        headers = [
            _scalar(value, f"{item_name}.headers[{column}]")
            for column, value in enumerate(
                _sequence(item.get("headers", []), f"{item_name}.headers")
            )
        ]
        rows = [
            _row_values(row, f"{item_name}.rows[{row_index}]")
            for row_index, row in enumerate(
                _sequence(item.get("rows"), f"{item_name}.rows")
            )
        ]
        if not headers and not rows:
            raise OfficeInputError(f"{item_name} 不能为空。", f"{item_name} cannot be empty.")
        columns = max([len(headers), *(len(row) for row in rows)])
        if columns > MAX_PPTX_TABLE_COLUMNS:
            raise OfficeInputError(
                f"PPTX 表格最多支持 {MAX_PPTX_TABLE_COLUMNS} 列。",
                f"PPTX tables support at most {MAX_PPTX_TABLE_COLUMNS} columns.",
            )
        total_cells += columns * (len(rows) + (1 if headers else 0))
        if total_cells > MAX_PPTX_TABLE_CELLS_PER_SLIDE:
            raise OfficeInputError(
                f"单张幻灯片的表格最多包含 {MAX_PPTX_TABLE_CELLS_PER_SLIDE} 个单元格。",
                (
                    "Tables on one slide support at most "
                    f"{MAX_PPTX_TABLE_CELLS_PER_SLIDE} cells."
                ),
            )
        parsed.append(
            {
                "headers": headers,
                "rows": rows,
                "left_inches": _bounded_number(
                    item.get("left_inches"),
                    f"{item_name}.left_inches",
                    minimum=0,
                    maximum=100,
                ),
                "top_inches": _bounded_number(
                    item.get("top_inches"),
                    f"{item_name}.top_inches",
                    minimum=0,
                    maximum=100,
                ),
                "width_inches": _bounded_number(
                    item.get("width_inches"),
                    f"{item_name}.width_inches",
                    minimum=0.1,
                    maximum=100,
                ),
                "height_inches": _bounded_number(
                    item.get("height_inches"),
                    f"{item_name}.height_inches",
                    minimum=0.1,
                    maximum=100,
                ),
            }
        )
    return parsed


def _parse_pptx_images(
    value: Any,
    workspace: Path,
    staged_workspace: Path,
    name: str,
) -> list[dict[str, Any]]:
    items = _sequence(value, name)
    if len(items) > MAX_IMAGES_PER_FILE:
        raise OfficeInputError(
            f"单个 Office 文件最多添加 {MAX_IMAGES_PER_FILE} 张图片。",
            f"At most {MAX_IMAGES_PER_FILE} images may be added to one Office file.",
        )
    parsed: list[dict[str, Any]] = []
    for index, raw in enumerate(items):
        item_name = f"{name}[{index}]"
        item = _mapping(raw, item_name)
        image = _read_local_image(
            item.get("path"),
            workspace,
            staged_workspace,
            f"{item_name}.path",
        )
        width = (
            _bounded_number(
                item["width_inches"],
                f"{item_name}.width_inches",
                minimum=0.1,
                maximum=100,
            )
            if item.get("width_inches") is not None
            else None
        )
        height = (
            _bounded_number(
                item["height_inches"],
                f"{item_name}.height_inches",
                minimum=0.1,
                maximum=100,
            )
            if item.get("height_inches") is not None
            else None
        )
        parsed.append(
            {
                **image,
                "left_inches": _bounded_number(
                    item.get("left_inches"),
                    f"{item_name}.left_inches",
                    minimum=0,
                    maximum=100,
                ),
                "top_inches": _bounded_number(
                    item.get("top_inches"),
                    f"{item_name}.top_inches",
                    minimum=0,
                    maximum=100,
                ),
                "width_inches": width,
                "height_inches": height,
            }
        )
    return parsed


def _parse_bullets(value: Any, name: str) -> list[dict[str, Any]]:
    bullets = _sequence(value, name)
    if len(bullets) > MAX_BULLETS_PER_SLIDE:
        raise OfficeInputError(
            f"单张幻灯片最多支持 {MAX_BULLETS_PER_SLIDE} 个项目符号。",
            f"A slide supports at most {MAX_BULLETS_PER_SLIDE} bullets.",
        )
    result: list[dict[str, Any]] = []
    for index, raw in enumerate(bullets):
        if isinstance(raw, str):
            result.append({"text": _bounded_text(raw, f"{name}[{index}]"), "level": 0})
            continue
        item = _mapping(raw, f"{name}[{index}]")
        text = _required_text(item.get("text"), f"{name}[{index}].text")
        level = item.get("level", 0)
        if isinstance(level, bool) or not isinstance(level, int) or not 0 <= level <= 4:
            raise OfficeInputError(
                f"{name}[{index}].level 必须是 0 到 4 的整数。",
                f"{name}[{index}].level must be an integer from 0 through 4.",
            )
        result.append({"text": text, "level": level})
    return result


def _parse_replacements(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    replacements = _sequence(value, "replacements")
    if len(replacements) > MAX_REPLACEMENTS:
        raise OfficeInputError(
            f"单次最多执行 {MAX_REPLACEMENTS} 项文本替换。",
            f"At most {MAX_REPLACEMENTS} replacements may be made in one call.",
        )
    parsed: list[dict[str, Any]] = []
    for index, raw in enumerate(replacements):
        item = _mapping(raw, f"replacements[{index}]")
        old_text = _required_text(item.get("old_text"), f"replacements[{index}].old_text")
        new_text = _optional_text(item.get("new_text"), f"replacements[{index}].new_text")
        if new_text is None:
            raise OfficeInputError(
                f"replacements[{index}].new_text 不能缺失。",
                f"replacements[{index}].new_text is required.",
            )
        if old_text == new_text:
            raise OfficeInputError(
                f"replacements[{index}] 的新旧文本相同。",
                f"replacements[{index}] old_text and new_text are identical.",
            )
        replace_all = item.get("replace_all", False)
        if not isinstance(replace_all, bool):
            raise OfficeInputError(
                f"replacements[{index}].replace_all 必须是布尔值。",
                f"replacements[{index}].replace_all must be a boolean.",
            )
        parsed.append(
            {"old_text": old_text, "new_text": new_text, "replace_all": replace_all}
        )
    return parsed


def _apply_replacements(
    paragraphs: list[Any],
    replacements: Sequence[Mapping[str, Any]],
    format_name: str,
) -> int:
    total = 0
    for index, replacement in enumerate(replacements):
        old_text = replacement["old_text"]
        occurrences = sum(_paragraph_run_text(paragraph).count(old_text) for paragraph in paragraphs)
        if occurrences == 0:
            raise OfficeInputError(
                f"{format_name} 中找不到 replacements[{index}].old_text。",
                f"replacements[{index}].old_text was not found in the {format_name} file.",
            )
        if occurrences > 1 and not replacement["replace_all"]:
            raise OfficeInputError(
                f"{format_name} 中找到 {occurrences} 处匹配；请提供更唯一的文本或设置 replace_all=true。",
                (
                    f"Found {occurrences} matches in the {format_name} file; provide "
                    "more unique text or set replace_all=true."
                ),
            )
        remaining = occurrences if replacement["replace_all"] else 1
        for paragraph in paragraphs:
            if remaining <= 0:
                break
            replaced = _replace_in_runs(
                paragraph,
                old_text,
                replacement["new_text"],
                limit=remaining,
            )
            remaining -= replaced
            total += replaced
    return total


def _replace_in_runs(paragraph: Any, old: str, new: str, *, limit: int) -> int:
    runs = list(paragraph.runs)
    text = "".join(run.text or "" for run in runs)
    positions: list[int] = []
    cursor = 0
    while len(positions) < limit:
        position = text.find(old, cursor)
        if position < 0:
            break
        positions.append(position)
        cursor = position + len(old)
    if not positions:
        return 0

    for start in reversed(positions):
        end = start + len(old)
        offsets: list[tuple[int, int]] = []
        offset = 0
        for run_index, run in enumerate(runs):
            next_offset = offset + len(run.text or "")
            offsets.append((offset, next_offset))
            offset = next_offset
        start_run = next(
            index
            for index, (run_start, run_end) in enumerate(offsets)
            if run_start <= start < run_end
        )
        end_run = next(
            index
            for index, (run_start, run_end) in enumerate(offsets)
            if run_start < end <= run_end
        )
        start_offset = start - offsets[start_run][0]
        end_offset = end - offsets[end_run][0]
        if start_run == end_run:
            run_text = runs[start_run].text or ""
            runs[start_run].text = run_text[:start_offset] + new + run_text[end_offset:]
        else:
            start_text = runs[start_run].text or ""
            end_text = runs[end_run].text or ""
            runs[start_run].text = start_text[:start_offset] + new
            for run_index in range(start_run + 1, end_run):
                runs[run_index].text = ""
            runs[end_run].text = end_text[end_offset:]
        text = text[:start] + new + text[end:]
    return len(positions)


def _iter_docx_paragraphs(document: Any) -> Iterator[Any]:
    yield from document.paragraphs
    for table in document.tables:
        yield from _iter_docx_table_paragraphs(table)


def _iter_docx_table_paragraphs(table: Any) -> Iterator[Any]:
    for row in table.rows:
        for cell in row.cells:
            yield from cell.paragraphs
            for nested in cell.tables:
                yield from _iter_docx_table_paragraphs(nested)


def _iter_pptx_paragraphs(presentation: Any) -> Iterator[Any]:
    for slide in presentation.slides:
        for shape in slide.shapes:
            yield from _iter_pptx_shape_paragraphs(shape)


def _iter_pptx_shape_paragraphs(shape: Any) -> Iterator[Any]:
    if getattr(shape, "has_text_frame", False):
        yield from shape.text_frame.paragraphs
    if getattr(shape, "has_table", False):
        for row in shape.table.rows:
            for cell in row.cells:
                yield from cell.text_frame.paragraphs
    if hasattr(shape, "shapes"):
        for child in shape.shapes:
            yield from _iter_pptx_shape_paragraphs(child)


def _paragraph_run_text(paragraph: Any) -> str:
    return "".join(run.text or "" for run in paragraph.runs)


def _docx_semantic(document: Any) -> Iterable[str]:
    for paragraph in _iter_docx_paragraphs(document):
        yield _paragraph_run_text(paragraph)


def _docx_page_break_count(document: Any) -> int:
    from docx.oxml.ns import qn

    page_type = qn("w:type")
    return sum(
        1
        for element in document.element.iter(qn("w:br"))
        if element.get(page_type) == "page"
    )


def _pptx_semantic(presentation: Any) -> Iterable[str]:
    for paragraph in _iter_pptx_paragraphs(presentation):
        yield _paragraph_run_text(paragraph)


def _pptx_shape_counts(presentation: Any) -> dict[str, int]:
    from pptx.enum.shapes import MSO_SHAPE_TYPE

    counts = {"pictures": 0, "tables": 0, "text_boxes": 0}

    def visit(shape: Any) -> None:
        if shape.shape_type == MSO_SHAPE_TYPE.PICTURE:
            counts["pictures"] += 1
        if getattr(shape, "has_table", False):
            counts["tables"] += 1
        if shape.shape_type == MSO_SHAPE_TYPE.TEXT_BOX:
            counts["text_boxes"] += 1
        if hasattr(shape, "shapes"):
            for child in shape.shapes:
                visit(child)

    for slide in presentation.slides:
        for shape in slide.shapes:
            visit(shape)
    return counts


def _semantic_digest(values: Iterable[str]) -> str:
    digest = hashlib.sha256()
    for value in values:
        encoded = value.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def _add_slide(
    presentation: Any,
    title: str,
    subtitle: str | None,
    bullets: Sequence[Mapping[str, Any]],
    text_boxes: Sequence[Mapping[str, Any]],
    tables: Sequence[Mapping[str, Any]],
    images: Sequence[Mapping[str, Any]],
) -> None:
    from pptx.util import Inches, Pt

    if subtitle is not None and not bullets:
        layout_index = 0
    elif bullets:
        layout_index = 1
    else:
        layout_index = 5 if len(presentation.slide_layouts) > 5 else 0
    slide = presentation.slides.add_slide(presentation.slide_layouts[layout_index])
    if slide.shapes.title is not None:
        slide.shapes.title.text = title
    if subtitle is not None:
        for placeholder in slide.placeholders:
            if placeholder == slide.shapes.title or not getattr(
                placeholder, "has_text_frame", False
            ):
                continue
            placeholder.text = subtitle
            break
    if bullets:
        body = next(
            (
                placeholder
                for placeholder in slide.placeholders
                if placeholder != slide.shapes.title
                and getattr(placeholder, "has_text_frame", False)
            ),
            None,
        )
        if body is None:
            raise OfficeInputError(
                "PPTX 默认布局中缺少正文占位符。",
                "The default PPTX layout has no body placeholder.",
            )
        frame = body.text_frame
        frame.clear()
        for index, bullet in enumerate(bullets):
            paragraph = frame.paragraphs[0] if index == 0 else frame.add_paragraph()
            paragraph.text = bullet["text"]
            paragraph.level = bullet["level"]
    for text_box in text_boxes:
        shape = slide.shapes.add_textbox(
            Inches(text_box["left_inches"]),
            Inches(text_box["top_inches"]),
            Inches(text_box["width_inches"]),
            Inches(text_box["height_inches"]),
        )
        shape.text_frame.text = text_box["text"]
        if text_box["font_size"] is not None:
            for paragraph in shape.text_frame.paragraphs:
                for run in paragraph.runs:
                    run.font.size = Pt(text_box["font_size"])
    for table_data in tables:
        headers = table_data["headers"]
        rows = table_data["rows"]
        column_count = max([len(headers), *(len(row) for row in rows)])
        row_count = len(rows) + (1 if headers else 0)
        shape = slide.shapes.add_table(
            row_count,
            column_count,
            Inches(table_data["left_inches"]),
            Inches(table_data["top_inches"]),
            Inches(table_data["width_inches"]),
            Inches(table_data["height_inches"]),
        )
        table = shape.table
        output_row = 0
        if headers:
            for column, value in enumerate(headers):
                table.cell(0, column).text = _cell_text(value)
            output_row = 1
        for row_index, row in enumerate(rows, output_row):
            for column, value in enumerate(row):
                table.cell(row_index, column).text = _cell_text(value)
    for image in images:
        width = Inches(image["width_inches"]) if image["width_inches"] else None
        height = Inches(image["height_inches"]) if image["height_inches"] else None
        slide.shapes.add_picture(
            io.BytesIO(image["data"]),
            Inches(image["left_inches"]),
            Inches(image["top_inches"]),
            width=width,
            height=height,
        )


def _validate_sheet_name(name: str) -> None:
    if len(name) > 31 or _INVALID_SHEET_TITLE.search(name) or name.startswith("'") or name.endswith("'"):
        raise OfficeInputError(
            f"无效的 XLSX 工作表名：{name}",
            f"Invalid XLSX sheet name: {name}",
        )


def _apply_xlsx_style(cell: Any, value: Any, name: str) -> None:
    from openpyxl.styles import PatternFill

    style = _mapping(value, name)
    unknown = sorted(set(style) - {"number_format", "font", "fill"})
    if unknown:
        raise OfficeInputError(
            f"{name} 包含不支持的样式项：{', '.join(unknown)}",
            f"{name} contains unsupported style fields: {', '.join(unknown)}",
        )
    if not style:
        raise OfficeInputError(f"{name} 不能为空。", f"{name} cannot be empty.")
    if "number_format" in style:
        number_format = _required_text(style["number_format"], f"{name}.number_format")
        if len(number_format) > 255:
            raise OfficeInputError(
                f"{name}.number_format 超过 255 个字符。",
                f"{name}.number_format exceeds 255 characters.",
            )
        cell.number_format = number_format
    if "font" in style:
        font_data = _mapping(style["font"], f"{name}.font")
        unknown_font = sorted(set(font_data) - {"bold", "italic", "color", "size"})
        if unknown_font:
            raise OfficeInputError(
                f"{name}.font 包含不支持的字段：{', '.join(unknown_font)}",
                f"{name}.font contains unsupported fields: {', '.join(unknown_font)}",
            )
        if not font_data:
            raise OfficeInputError(
                f"{name}.font 不能为空。", f"{name}.font cannot be empty."
            )
        font = copy.copy(cell.font)
        for field in ("bold", "italic"):
            if field in font_data:
                if not isinstance(font_data[field], bool):
                    raise OfficeInputError(
                        f"{name}.font.{field} 必须是布尔值。",
                        f"{name}.font.{field} must be a boolean.",
                    )
                setattr(font, field, font_data[field])
        if "color" in font_data:
            font.color = _validate_hex_color(font_data["color"], f"{name}.font.color")
        if "size" in font_data:
            font.size = _bounded_number(
                font_data["size"], f"{name}.font.size", minimum=1, maximum=200
            )
        cell.font = font
    if "fill" in style:
        fill_data = _mapping(style["fill"], f"{name}.fill")
        if set(fill_data) != {"color"}:
            raise OfficeInputError(
                f"{name}.fill 只支持 color。",
                f"{name}.fill supports only color.",
            )
        color = _validate_hex_color(fill_data["color"], f"{name}.fill.color")
        cell.fill = PatternFill(fill_type="solid", fgColor=color)


def _validate_hex_color(value: Any, name: str) -> str:
    if not isinstance(value, str) or not _HEX_COLOR.fullmatch(value):
        raise OfficeInputError(
            f"{name} 必须是 6 或 8 位十六进制颜色。",
            f"{name} must be a 6- or 8-digit hexadecimal color.",
        )
    return value.upper()


def _xlsx_color_snapshot(color: Any) -> dict[str, Any] | None:
    if color is None:
        return None
    return {
        "type": color.type,
        "rgb": color.rgb if color.type == "rgb" else None,
        "indexed": color.indexed if color.type == "indexed" else None,
        "theme": color.theme if color.type == "theme" else None,
        "tint": float(color.tint or 0),
    }


def _xlsx_style_snapshot(cell: Any) -> dict[str, Any]:
    return {
        "number_format": cell.number_format,
        "font": {
            "bold": bool(cell.font.bold),
            "italic": bool(cell.font.italic),
            "size": float(cell.font.size) if cell.font.size is not None else None,
            "color": _xlsx_color_snapshot(cell.font.color),
        },
        "fill": {
            "fill_type": cell.fill.fill_type,
            "fg_color": _xlsx_color_snapshot(cell.fill.fgColor),
        },
    }


def _cell_text(value: Any) -> str:
    return "" if value is None else str(value)


def _is_formula(value: Any) -> bool:
    return isinstance(value, str) and value.startswith("=")


def _flush_file(path: Path) -> None:
    # Windows implements os.fsync() with the CRT commit operation, which
    # rejects a descriptor opened read-only with EBADF.  The Office output is
    # still our private, writable temporary at this point, so open it for
    # update before making the durability barrier.
    with path.open("r+b") as handle:
        os.fsync(handle.fileno())


def _atomic_replace(source: Path, target: Path) -> None:
    """Single-filesystem install seam, kept small for fault-injection tests."""

    os.replace(source, target)


def _fsync_directory(directory: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    try:
        directory_fd = os.open(directory, flags)
    except OSError:
        return
    try:
        os.fsync(directory_fd)
    except OSError:
        pass
    finally:
        os.close(directory_fd)
