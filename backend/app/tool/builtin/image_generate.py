"""Built-in, cost-confirmed SiliconFlow text-to-image tool."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.dependencies import get_provider_registry, get_settings
from app.image_generation import (
    ImageGenerationBillingUncertain,
    ImageGenerationCancelled,
    ImageGenerationError,
    ImageGenerationLedger,
    ImageGenerationLedgerError,
    SiliconFlowImageClient,
)
from app.image_generation.siliconflow import (
    MAX_IMAGE_BYTES,
    SILICONFLOW_IMAGE_ESTIMATED_COST_CNY,
    SILICONFLOW_IMAGE_MODEL,
    SILICONFLOW_IMAGE_PRICING_AS_OF,
    SILICONFLOW_IMAGE_PRICING_SOURCE_URL,
    SILICONFLOW_IMAGE_SIZES,
    _parse_png,
)
from app.tool.base import ToolDefinition, ToolResult
from app.tool.context import ToolContext
from app.tool.workspace import WorkspaceViolation, resolve_and_validate, resolve_for_write
from app.tool.workspace_transaction import (
    WorkspaceMutationError,
    WorkspaceMutationTransaction,
)
from app.utils.atomic_write import _fsync_directory
from app.utils.id import generate_ulid


def _atomic_write_new(path: Path, content: bytes) -> None:
    """Create a new binary file atomically without overwriting an existing file."""
    parent = path.parent
    temporary: Path | None = None
    fd = -1
    for _ in range(100):
        candidate = parent / f".{path.name}.{secrets.token_hex(8)}.tmp"
        try:
            fd = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        temporary = candidate
        break
    if temporary is None:
        raise FileExistsError(f"Could not allocate a temporary file beside {path}")

    try:
        with os.fdopen(fd, "wb") as handle:
            fd = -1
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        # Installing through a hard link is atomic and fails if another writer
        # created the destination after our early existence check.  Unlike
        # os.replace(), it can never overwrite an existing user file.
        os.link(temporary, path)
        temporary.unlink()
        temporary = None
        _fsync_directory(parent)
    finally:
        if fd >= 0:
            os.close(fd)
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def _billing_metadata(ctx: ToolContext) -> dict[str, Any]:
    """Return the stable provider/model/cost contract consumed by the UI.

    SiliconFlow's image response does not include the charged amount.  The
    numeric value is therefore the official catalog estimate as of the stated
    date, never represented as an actual charge.
    """

    return {
        "provider": "siliconflow",
        "provider_name": "SiliconFlow",
        "model": SILICONFLOW_IMAGE_MODEL,
        "estimated_cost": SILICONFLOW_IMAGE_ESTIMATED_COST_CNY,
        "currency": "CNY",
        "pricing_unit": "image",
        "pricing_basis": "official_catalog",
        "pricing_as_of": SILICONFLOW_IMAGE_PRICING_AS_OF,
        "pricing_source_url": SILICONFLOW_IMAGE_PRICING_SOURCE_URL,
        "approval_mode": "per_call",
        "external_billing": True,
        "cost_notice": ctx.tr(
            "目录估价可能变化，最终费用以供应商账单为准",
            "Catalog pricing may change; the provider bill is authoritative",
        ),
    }


class ImageGenerateTool(ToolDefinition):
    @property
    def id(self) -> str:
        return "image_generate"

    @property
    def description(self) -> str:
        return (
            "Generate one PNG image with the configured SiliconFlow account and save it "
            "inside the current workspace. This is a paid external action and always "
            "requires user permission. It supports text-to-image only; do not use it "
            "for image editing, video, or bulk generation."
        )

    @property
    def requires_approval(self) -> bool:
        """Provider billing is a non-overridable per-call approval boundary."""

        return True

    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "Detailed text description of the image to generate",
                    "minLength": 1,
                    "maxLength": 4000,
                },
                "output_path": {
                    "type": "string",
                    "description": (
                        "Optional workspace-relative .png path. Relative paths are saved "
                        "under suxiaoyou_written. Existing files are never overwritten."
                    ),
                },
                "image_size": {
                    "type": "string",
                    "enum": sorted(SILICONFLOW_IMAGE_SIZES),
                    "default": "1024x1024",
                },
                "negative_prompt": {
                    "type": "string",
                    "description": "Optional elements to avoid, at most 2000 characters",
                },
                "seed": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 9999999999,
                },
                "num_inference_steps": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 100,
                    "default": 20,
                },
                "guidance_scale": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 20,
                    "default": 7.5,
                },
            },
            "required": ["prompt"],
        }

    async def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        if not ctx.workspace:
            return ToolResult(error=ctx.tr("生成图片前必须选择工作区。", "Select a workspace before generating images."))
        if ctx.is_aborted:
            return ToolResult(error=ctx.tr("图片生成已取消。", "Image generation was cancelled."))

        registry = get_provider_registry()
        if registry.get_provider("siliconflow") is None:
            return ToolResult(
                error=ctx.tr(
                    "请先在模型供应商设置中配置并启用硅基流动 API Key。",
                    "Configure and enable a SiliconFlow API key in provider settings first.",
                )
            )
        from app.auth.credential_store import resolve_env_value

        protected_api_key = get_settings().siliconflow_api_key.strip()
        api_key = resolve_env_value(
            "SUXIAOYOU_SILICONFLOW_API_KEY",
            protected_api_key,
        ).strip()
        if not api_key:
            return ToolResult(error=ctx.tr("硅基流动凭据不可用。", "SiliconFlow credentials are unavailable."))

        requested = args.get("output_path")
        if not requested:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            requested = f"generated-{stamp}-{ctx.call_id[:8]}.png"
        if Path(requested).suffix.lower() != ".png":
            return ToolResult(error=ctx.tr("图片输出路径必须以 .png 结尾。", "Image output path must end in .png."))
        try:
            resolved = Path(resolve_for_write(requested, ctx.workspace))
        except WorkspaceViolation as exc:
            return ToolResult(error=str(exc))

        # Treat the generated artifact like every other declarative workspace
        # mutation.  Preparing a sparse private view before the paid request
        # anchors the workspace/parent identities, rejects redirected targets,
        # and lets commit install the new file with the same descriptor-based
        # no-replace protocol used by write/edit/Office.  No user-visible
        # directory or file is created before the provider call succeeds.
        transaction: WorkspaceMutationTransaction | None = None
        try:
            transaction = WorkspaceMutationTransaction(
                ctx.workspace,
                ctx,
                operation="image_generate",
            )
            transaction.prepare_paths([resolved])
            staged_resolved = transaction.staged_path(resolved)
        except WorkspaceMutationError as exc:
            if transaction is not None:
                transaction.abort()
            return ToolResult(error=str(exc), metadata={"billing_status": "blocked"})

        prompt_digest = hashlib.sha256(args["prompt"].encode("utf-8")).hexdigest()
        parameters_digest = hashlib.sha256(
            json.dumps(
                {
                    "image_size": args.get("image_size", "1024x1024"),
                    "negative_prompt": args.get("negative_prompt"),
                    "seed": args.get("seed"),
                    "num_inference_steps": args.get("num_inference_steps", 20),
                    "guidance_scale": args.get("guidance_scale", 7.5),
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        ledger = ImageGenerationLedger()
        try:
            operation, created = ledger.begin({
                "call_id": ctx.call_id,
                "model": SILICONFLOW_IMAGE_MODEL,
                "prompt_sha256": prompt_digest,
                "parameters_sha256": parameters_digest,
                "output_path": str(resolved),
                "image_size": args.get("image_size", "1024x1024"),
            })
        except ImageGenerationLedgerError as exc:
            transaction.abort()
            return ToolResult(error=str(exc), metadata={"billing_status": "blocked"})
        if not created:
            transaction.abort()
            return _replayed_operation_result(operation, ctx)
        if staged_resolved.exists():
            transaction.abort()
            _mark_ledger_safely(ledger, ctx.call_id, "rejected")
            return ToolResult(
                error=ctx.tr(
                    f"不会覆盖已有文件：{resolved}",
                    f"Refusing to overwrite existing file: {resolved}",
                ),
                metadata={
                    "billing_status": "rejected",
                    "operation_id": ctx.call_id,
                },
            )

        ctx.publish_metadata(
            title=ctx.tr("正在生成图片", "Generating image"),
            metadata={
                **_billing_metadata(ctx),
                "status": "running",
                "billing_status": "submitted",
                "operation_id": ctx.call_id,
                "image_size": args.get("image_size", "1024x1024"),
            },
        )

        client = SiliconFlowImageClient(api_key)
        generated = None
        try:
            generated = await client.generate(
                prompt=args["prompt"],
                image_size=args.get("image_size", "1024x1024"),
                negative_prompt=args.get("negative_prompt"),
                seed=args.get("seed"),
                num_inference_steps=args.get("num_inference_steps", 20),
                guidance_scale=args.get("guidance_scale", 7.5),
                abort_event=ctx.abort_event,
            )
            staged_resolved.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write_new(staged_resolved, generated.content)
            commit = transaction.commit()
        except ImageGenerationBillingUncertain as exc:
            transaction.abort()
            _mark_ledger_safely(ledger, ctx.call_id, "uncertain")
            return ToolResult(
                error=str(exc),
                metadata={
                    "status": "failed",
                    "billing_status": "uncertain",
                    "operation_id": ctx.call_id,
                    "retry_requires_new_confirmation": True,
                },
            )
        except ImageGenerationCancelled:
            transaction.abort()
            _mark_ledger_safely(ledger, ctx.call_id, "uncertain")
            return ToolResult(
                error=ctx.tr(
                    "图片请求已取消，但供应商可能已经接受并计费；再次生成前请先核对账单。",
                    "The image request was cancelled, but the provider may have accepted and charged it; check billing before retrying.",
                ),
                metadata={
                    "status": "cancelled",
                    "billing_status": "uncertain",
                    "operation_id": ctx.call_id,
                    "retry_requires_new_confirmation": True,
                },
            )
        except ImageGenerationError as exc:
            transaction.abort()
            _mark_ledger_safely(ledger, ctx.call_id, "rejected")
            return ToolResult(
                error=str(exc),
                metadata={
                    "status": "failed",
                    "billing_status": "rejected",
                    "operation_id": ctx.call_id,
                },
            )
        except (OSError, WorkspaceMutationError):
            transaction.abort()
            status = "output_failed" if generated is not None else "uncertain"
            _mark_ledger_safely(ledger, ctx.call_id, status)
            return ToolResult(
                error=ctx.tr(
                    "供应商可能已完成并计费，但本地图片保存失败；请勿直接重试，先核对账单。",
                    "The provider may have completed and charged the request, but local saving failed; check billing before retrying.",
                ),
                metadata={
                    "status": "failed",
                    "billing_status": status,
                    "operation_id": ctx.call_id,
                    "retry_requires_new_confirmation": True,
                },
            )
        finally:
            try:
                await client.aclose()
            except Exception:
                # Closing a client cannot change the already-recorded billing
                # outcome and must not replace a safe, redacted tool result.
                pass

        digest = hashlib.sha256(generated.content).hexdigest()
        ledger_warning = not _mark_ledger_safely(
            ledger,
            ctx.call_id,
            "completed",
            content_hash=digest,
            width=generated.width,
            height=generated.height,
            bytes=len(generated.content),
            seed=generated.seed,
            trace_id=generated.trace_id,
        )
        ctx.publish_metadata(
            title=ctx.tr("图片已生成", "Image generated"),
            metadata={
                **_billing_metadata(ctx),
                "status": "completed",
                "billing_status": "completed",
                "operation_id": ctx.call_id,
                "file_path": str(resolved),
            },
        )
        return ToolResult(
            output=ctx.tr(
                f"已使用硅基流动 {SILICONFLOW_IMAGE_MODEL} 生成并保存图片：{resolved}",
                f"Generated and saved an image with SiliconFlow {SILICONFLOW_IMAGE_MODEL}: {resolved}",
            ),
            title=ctx.tr(f"已生成 {resolved.name}", f"Generated {resolved.name}"),
            metadata={
                **_billing_metadata(ctx),
                "status": "completed",
                "billing_status": "completed",
                "operation_id": ctx.call_id,
                "ledger_warning": ledger_warning,
                "image_size": f"{generated.width}x{generated.height}",
                "seed": generated.seed,
                "trace_id": generated.trace_id,
                "file_path": str(resolved),
                "content_hash": digest,
                "prompt_sha256": prompt_digest,
                "parameters_sha256": parameters_digest,
                **commit.metadata,
            },
            attachments=[
                {
                    "file_id": generate_ulid(),
                    "name": resolved.name,
                    "path": str(resolved),
                    "size": len(generated.content),
                    "mime_type": "image/png",
                    "source": "referenced",
                    "content_hash": digest,
                }
            ],
        )


def _mark_ledger_safely(
    ledger: ImageGenerationLedger,
    call_id: str,
    status: str,
    **metadata: Any,
) -> bool:
    try:
        ledger.mark(call_id, status, **metadata)
        return True
    except ImageGenerationLedgerError:
        # The existing ``submitted`` row still prevents a silent replay of the
        # same paid operation.  The result surfaces ``ledger_warning`` so the
        # user knows the bookkeeping needs attention.
        return False


def _replayed_operation_result(operation: dict[str, Any], ctx: ToolContext) -> ToolResult:
    status = str(operation.get("status", "uncertain"))
    if status != "completed":
        return ToolResult(
            error=ctx.tr(
                f"该计费图片操作已经记录为 {status}；请先核对供应商账单，再以新的明确确认发起请求。",
                f"This paid image operation is already recorded as {status}; check provider billing before making a newly confirmed request.",
            ),
            metadata={
                "status": "blocked",
                "billing_status": status,
                "operation_id": operation.get("call_id"),
                "replayed": True,
            },
        )

    try:
        path = Path(resolve_and_validate(str(operation.get("output_path", "")), ctx.workspace))
    except (WorkspaceViolation, OSError):
        path = Path()
    expected_hash = str(operation.get("content_hash", ""))
    try:
        if not path.is_file() or path.is_symlink() or path.stat().st_size > MAX_IMAGE_BYTES:
            raise OSError("invalid replay artifact")
        content = path.read_bytes()
        _parse_png(content)
    except (ImageGenerationError, OSError):
        content = b""
    digest = hashlib.sha256(content).hexdigest() if content else ""
    if not content or digest != expected_hash:
        return ToolResult(
            error=ctx.tr(
                "该计费图片操作已完成，但原产物缺失或校验失败；为避免重复计费，不会自动重试。",
                "This paid image operation completed, but its artifact is missing or invalid; it will not be retried automatically.",
            ),
            metadata={
                "status": "blocked",
                "billing_status": "completed",
                "operation_id": operation.get("call_id"),
                "replayed": True,
            },
        )
    return ToolResult(
        output=ctx.tr(
            f"该计费操作已完成，复用已有图片：{path}",
            f"This paid operation already completed; reused the existing image: {path}",
        ),
        title=ctx.tr(f"已生成 {path.name}", f"Generated {path.name}"),
        metadata={
            **_billing_metadata(ctx),
            "status": "completed",
            "billing_status": "completed",
            "operation_id": operation.get("call_id"),
            "file_path": str(path),
            "content_hash": digest,
            "replayed": True,
        },
        attachments=[{
            "file_id": generate_ulid(),
            "name": path.name,
            "path": str(path),
            "size": len(content),
            "mime_type": "image/png",
            "source": "referenced",
            "content_hash": digest,
        }],
    )
