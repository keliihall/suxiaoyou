"""Server-owned coordination for deterministic Office precommit validation.

The coordinator consumes only a transaction-derived view.  Model arguments can
select a released template ID, but they can never provide paths, render policy,
thresholds, or a commit credential.  A concrete release composition must supply
an authoritative renderer and a trusted policy resolver; importing this module
does not make the Office v1.1 write path available.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
import stat
import threading
from typing import Literal, Protocol, runtime_checkable

from app.office_templates.models import TemplatePackageManifest
from app.office_validation.draft import (
    OfficeDraftArtifact,
    OfficeDraftSeal,
    OfficeDraftValidationResult,
    OfficeDraftValidationService,
    OfficeGoldenPolicy,
)
from app.office_validation.errors import (
    OfficeValidationContractError,
    OfficeValidationError,
)
from app.office_validation.visual import VisualDiffPolicy
from app.tool.workspace_transaction import (
    WorkspaceEntry,
    WorkspaceOfficePrecommitView,
)


OfficeOperation = Literal["create", "edit"]
OfficeDocumentFormat = Literal["docx", "xlsx", "pptx"]


class OfficePrecommitError(OfficeValidationError):
    """Base error for the private validation-to-commit coordination layer."""


class OfficePrecommitUnavailableError(OfficePrecommitError):
    """No trusted authoritative runtime/policy is configured."""


class OfficePrecommitRejectedError(OfficePrecommitError):
    """Deterministic validation did not produce an authoritative pass."""


class OfficePrecommitStateError(OfficePrecommitError):
    """A validation session or transaction view was reused out of order."""


@dataclass(frozen=True, slots=True)
class OfficePrecommitRequest:
    """Trusted runtime identity plus the narrow model-selected operation."""

    operation: OfficeOperation
    document_format: OfficeDocumentFormat
    relative_path: str
    session_id: str
    message_id: str
    call_id: str
    root_turn_id: str
    turn_run_id: str
    checkpoint_id: str
    workspace_instance_id: str
    template_id: str | None = None
    template_version: str | None = None
    trusted_create_plan: OfficeCreateValidationPlan | None = field(
        default=None,
        repr=False,
    )
    trusted_edit_intent: OfficeEditMutationIntent | None = field(
        default=None,
        repr=False,
    )

    def __post_init__(self) -> None:
        if self.operation not in {"create", "edit"}:
            raise OfficeValidationContractError(
                "Office precommit operation is invalid"
            )
        if self.document_format not in {"docx", "xlsx", "pptx"}:
            raise OfficeValidationContractError(
                "Office precommit document format is invalid"
            )
        if (
            not isinstance(self.relative_path, str)
            or not self.relative_path
            or len(self.relative_path) > 4096
            or "\\" in self.relative_path
        ):
            raise OfficeValidationContractError(
                "Office precommit relative path is invalid"
            )
        relative = PurePosixPath(self.relative_path)
        if relative.is_absolute() or any(
            part in {"", ".", ".."} for part in relative.parts
        ):
            raise OfficeValidationContractError(
                "Office precommit relative path is invalid"
            )
        if relative.suffix.casefold() != f".{self.document_format}":
            raise OfficeValidationContractError(
                "Office precommit path and format differ"
            )
        for value, field in (
            (self.session_id, "session_id"),
            (self.message_id, "message_id"),
            (self.call_id, "call_id"),
            (self.root_turn_id, "root_turn_id"),
            (self.turn_run_id, "turn_run_id"),
            (self.checkpoint_id, "checkpoint_id"),
            (self.workspace_instance_id, "workspace_instance_id"),
        ):
            if (
                not isinstance(value, str)
                or not value.strip()
                or len(value) > 200
                or any(ord(character) < 32 for character in value)
            ):
                raise OfficeValidationContractError(
                    f"Office precommit {field} is invalid"
                )
        if self.trusted_create_plan is not None:
            plan = self.trusted_create_plan
            if self.operation != "create" or not isinstance(
                plan,
                OfficeCreateValidationPlan,
            ):
                raise OfficeValidationContractError(
                    "Office trusted create plan is invalid"
                )
            if (
                self.template_id != plan.template_manifest.template_id
                or self.template_version
                != plan.template_manifest.template_version
            ):
                raise OfficeValidationContractError(
                    "Office trusted create plan identity differs from the request"
                )
        if self.trusted_edit_intent is not None:
            intent = self.trusted_edit_intent
            if (
                self.operation != "edit"
                or not isinstance(intent, OfficeEditMutationIntent)
                or intent.document_format != self.document_format
            ):
                raise OfficeValidationContractError(
                    "Office trusted edit intent is invalid"
                )
        if (self.template_id is None) != (self.template_version is None):
            raise OfficeValidationContractError(
                "Office precommit template identity is incomplete"
            )
        for value, field in (
            (self.template_id, "template_id"),
            (self.template_version, "template_version"),
        ):
            if value is not None and (
                not isinstance(value, str)
                or not value.strip()
                or len(value) > 128
                or any(ord(character) < 32 for character in value)
            ):
                raise OfficeValidationContractError(
                    f"Office precommit {field} is invalid"
                )


@dataclass(frozen=True, slots=True)
class OfficeEditMutationIntent:
    """Server-normalized mutation scope, never deserialized from tool input."""

    document_format: OfficeDocumentFormat
    max_added_pages: int
    max_removed_pages: int
    max_outside_changed_ratio: float
    max_total_changed_ratio: float
    max_blank_fraction_increase: float
    required_page_delta: int | None = None
    expected_logical_unit_delta: int | None = None

    def __post_init__(self) -> None:
        if self.document_format not in {"docx", "xlsx", "pptx"}:
            raise OfficeValidationContractError("Office edit intent format is invalid")
        for field_name in ("max_added_pages", "max_removed_pages"):
            value = getattr(self, field_name)
            if (
                not isinstance(value, int)
                or isinstance(value, bool)
                or not 0 <= value <= 50
            ):
                raise OfficeValidationContractError(
                    f"Office edit intent {field_name} is invalid"
                )
        for field_name in (
            "max_outside_changed_ratio",
            "max_total_changed_ratio",
            "max_blank_fraction_increase",
        ):
            value = getattr(self, field_name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not 0 <= float(value) <= 0.85
            ):
                raise OfficeValidationContractError(
                    f"Office edit intent {field_name} is invalid"
                )
        if self.required_page_delta is not None and (
            not isinstance(self.required_page_delta, int)
            or isinstance(self.required_page_delta, bool)
            or self.required_page_delta < -self.max_removed_pages
            or self.required_page_delta > self.max_added_pages
        ):
            raise OfficeValidationContractError(
                "Office edit intent required page delta is invalid"
            )
        if self.expected_logical_unit_delta is not None and (
            not isinstance(self.expected_logical_unit_delta, int)
            or isinstance(self.expected_logical_unit_delta, bool)
            or not -256 <= self.expected_logical_unit_delta <= 256
        ):
            raise OfficeValidationContractError(
                "Office edit intent logical unit delta is invalid"
            )


@dataclass(frozen=True, slots=True)
class OfficeEditValidationPlan:
    """Code-owned structural and visual envelope for one supported edit."""

    allowed_changed_parts: tuple[str, ...]
    visual_policy: VisualDiffPolicy
    expected_logical_unit_delta: int | None = None

    def __post_init__(self) -> None:
        try:
            allowed = tuple(self.allowed_changed_parts)
        except TypeError as exc:
            raise OfficeValidationContractError(
                "Office edit policy parts are invalid"
            ) from exc
        if (
            len(allowed) > 256
            or len(allowed) != len(set(allowed))
            or any(
                not isinstance(item, str)
                or not item
                or item.startswith("/")
                or "\\" in item
                or ".." in item.split("/")
                for item in allowed
            )
        ):
            raise OfficeValidationContractError(
                "Office edit policy parts are invalid"
            )
        if not isinstance(self.visual_policy, VisualDiffPolicy):
            raise OfficeValidationContractError(
                "Office edit visual policy is invalid"
            )
        if not self.visual_policy.require_authoritative:
            raise OfficeValidationContractError(
                "Office edit approval requires authoritative rendering"
            )
        if self.expected_logical_unit_delta is not None and (
            not isinstance(self.expected_logical_unit_delta, int)
            or isinstance(self.expected_logical_unit_delta, bool)
            or not -256 <= self.expected_logical_unit_delta <= 256
        ):
            raise OfficeValidationContractError(
                "Office edit logical unit delta is invalid"
            )
        object.__setattr__(self, "allowed_changed_parts", allowed)


@dataclass(frozen=True, slots=True)
class OfficeStandaloneCreateValidationPlan:
    """Code-owned policy for an ordinary create without a template golden."""

    policy_id: str
    document_format: OfficeDocumentFormat
    renderer_id: str
    renderer_version: str
    font_digest: str
    parameters_version: str
    parameters_sha256: str
    visual_policy: VisualDiffPolicy

    def __post_init__(self) -> None:
        for value, field_name in (
            (self.policy_id, "policy_id"),
            (self.renderer_id, "renderer_id"),
            (self.renderer_version, "renderer_version"),
            (self.parameters_version, "parameters_version"),
        ):
            if (
                not isinstance(value, str)
                or not value.strip()
                or len(value) > 256
                or any(ord(character) < 32 for character in value)
            ):
                raise OfficeValidationContractError(
                    f"Office standalone create {field_name} is invalid"
                )
        if self.document_format not in {"docx", "xlsx", "pptx"}:
            raise OfficeValidationContractError(
                "Office standalone create format is invalid"
            )
        for value, field_name in (
            (self.font_digest, "font_digest"),
            (self.parameters_sha256, "parameters_sha256"),
        ):
            if (
                not isinstance(value, str)
                or len(value) != 64
                or any(character not in "0123456789abcdef" for character in value)
            ):
                raise OfficeValidationContractError(
                    f"Office standalone create {field_name} is invalid"
                )
        if (
            not isinstance(self.visual_policy, VisualDiffPolicy)
            or not self.visual_policy.require_authoritative
        ):
            raise OfficeValidationContractError(
                "Office standalone create visual policy is invalid"
            )


@dataclass(frozen=True, slots=True)
class OfficeCreateValidationPlan:
    """Trusted signed golden and policy for one create operation."""

    golden_root: Path
    golden_path: Path
    golden_policy: OfficeGoldenPolicy
    template_manifest: TemplatePackageManifest

    def __post_init__(self) -> None:
        root = Path(self.golden_root).expanduser()
        path = Path(self.golden_path).expanduser()
        if not root.is_absolute() or not path.is_absolute():
            raise OfficeValidationContractError(
                "Office golden paths must be absolute"
            )
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise OfficeValidationContractError(
                "Office golden path escapes its root"
            ) from exc
        if not isinstance(self.golden_policy, OfficeGoldenPolicy) or not isinstance(
            self.template_manifest,
            TemplatePackageManifest,
        ):
            raise OfficeValidationContractError(
                "Office golden policy or manifest is invalid"
            )
        object.__setattr__(self, "golden_root", root)
        object.__setattr__(self, "golden_path", path)


@runtime_checkable
class OfficePrecommitPolicyResolver(Protocol):
    """Resolve only server-owned policy records, never request thresholds."""

    def resolve_edit(
        self,
        request: OfficePrecommitRequest,
        baseline: OfficeDraftArtifact,
    ) -> OfficeEditValidationPlan:
        ...

    def resolve_create(
        self,
        request: OfficePrecommitRequest,
    ) -> OfficeCreateValidationPlan:
        ...


@runtime_checkable
class OfficeStandaloneCreatePolicyResolver(Protocol):
    """Optional extension for ordinary creates without a template identity."""

    def resolve_standalone_create(
        self,
        request: OfficePrecommitRequest,
    ) -> OfficeStandaloneCreateValidationPlan:
        ...


@runtime_checkable
class OfficePrecommitValidationSession(Protocol):
    """Single-use validation result owner for one armed transaction view."""

    async def validate_candidate(self) -> OfficeDraftValidationResult:
        ...

    def consume_commit_seal(
        self,
        result: OfficeDraftValidationResult,
    ) -> OfficeDraftSeal:
        ...

    def mark_committed(self, result: OfficeDraftValidationResult) -> None:
        ...

    def abort(self) -> None:
        ...


@runtime_checkable
class OfficePrecommitCoordinator(Protocol):
    """Begin validation from a transaction-owned view and trusted request."""

    async def begin(
        self,
        *,
        request: OfficePrecommitRequest,
        view: WorkspaceOfficePrecommitView,
    ) -> OfficePrecommitValidationSession:
        ...


class DeterministicOfficePrecommitCoordinator:
    """Concrete draft service composition with a trusted policy resolver."""

    def __init__(
        self,
        *,
        service: OfficeDraftValidationService,
        policies: OfficePrecommitPolicyResolver,
    ) -> None:
        if not isinstance(service, OfficeDraftValidationService):
            raise TypeError("Office draft validation service is invalid")
        if not isinstance(policies, OfficePrecommitPolicyResolver):
            raise TypeError("Office precommit policy resolver is invalid")
        self._service = service
        self._policies = policies

    async def begin(
        self,
        *,
        request: OfficePrecommitRequest,
        view: WorkspaceOfficePrecommitView,
    ) -> OfficePrecommitValidationSession:
        _validate_request_view(request, view)
        if request.operation == "edit":
            baseline_entry = view.baseline
            if (
                baseline_entry is None
                or baseline_entry.kind != "file"
                or baseline_entry.sha256 is None
                or view.baseline_identity is None
            ):
                raise OfficePrecommitRejectedError(
                    "Office edit baseline is unavailable"
                )
            baseline = await self._service.capture(
                boundary_root=view.workspace_root,
                source_path=view.visible_target,
                expected_source_sha256=baseline_entry.sha256,
            )
            _validate_visible_baseline(view, baseline_entry, baseline)
            plan = await asyncio.to_thread(
                self._policies.resolve_edit,
                request,
                baseline,
            )
            if not isinstance(plan, OfficeEditValidationPlan):
                raise OfficeValidationContractError(
                    "Office edit policy resolver returned an invalid plan"
                )
            return _EditValidationSession(
                service=self._service,
                request=request,
                view=view,
                baseline=baseline,
                plan=plan,
            )

        plan = request.trusted_create_plan
        if (
            plan is None
            and request.template_id is None
            and request.template_version is None
        ):
            if not isinstance(
                self._policies,
                OfficeStandaloneCreatePolicyResolver,
            ):
                raise OfficePrecommitUnavailableError(
                    "Ordinary Office create validation is unavailable"
                )
            standalone_plan = await asyncio.to_thread(
                self._policies.resolve_standalone_create,
                request,
            )
            if not isinstance(
                standalone_plan,
                OfficeStandaloneCreateValidationPlan,
            ):
                raise OfficeValidationContractError(
                    "Office standalone create policy resolver returned an invalid plan"
                )
            if standalone_plan.document_format != request.document_format:
                raise OfficePrecommitRejectedError(
                    "Office standalone create policy format differs"
                )
            return _StandaloneCreateValidationSession(
                service=self._service,
                request=request,
                view=view,
                plan=standalone_plan,
            )
        if plan is None:
            # First-party create resolution revalidates signed catalog bytes and
            # a private content-addressed golden on every use.  Keep that
            # bounded filesystem/ZIP work off the server event loop.
            plan = await asyncio.to_thread(
                self._policies.resolve_create,
                request,
            )
        if not isinstance(plan, OfficeCreateValidationPlan):
            raise OfficeValidationContractError(
                "Office create policy resolver returned an invalid plan"
            )
        golden = await self._service.capture(
            boundary_root=plan.golden_root,
            source_path=plan.golden_path,
            expected_source_sha256=plan.golden_policy.baseline_sha256,
        )
        if golden.document_format != request.document_format:
            raise OfficePrecommitRejectedError(
                "Office golden format does not match the requested output"
            )
        return _CreateValidationSession(
            service=self._service,
            request=request,
            view=view,
            golden=golden,
            plan=plan,
        )


class _ValidationSessionBase:
    def __init__(
        self,
        *,
        service: OfficeDraftValidationService,
        request: OfficePrecommitRequest,
        view: WorkspaceOfficePrecommitView,
    ) -> None:
        self._service = service
        self._request = request
        self._view = view
        self._state = "begun"
        self._latest_result: OfficeDraftValidationResult | None = None

    async def validate_candidate(self) -> OfficeDraftValidationResult:
        if self._state != "begun":
            raise OfficePrecommitStateError(
                "Office precommit validation session is not reusable"
            )
        self._state = "validating"
        try:
            candidate = await self._service.capture(
                boundary_root=self._view.staged_root,
                source_path=self._view.staged_target,
            )
            _validate_candidate_identity(self._request, self._view, candidate)
            raw_result = self._compare(candidate)
            bound_candidate = replace(
                raw_result.candidate,
                validation_generation=self._view.validation_generation,
            )
            report = replace(
                raw_result.report,
                checkpoint_id=self._request.checkpoint_id,
                root_turn_id=self._request.root_turn_id,
            )
            result = OfficeDraftValidationResult(
                report=report,
                candidate=bound_candidate,
            )
        except BaseException:
            self._state = "aborted"
            raise
        self._latest_result = result
        self._state = "validated"
        return result

    def _compare(
        self,
        candidate: OfficeDraftArtifact,
    ) -> OfficeDraftValidationResult:
        raise NotImplementedError

    def consume_commit_seal(
        self,
        result: OfficeDraftValidationResult,
    ) -> OfficeDraftSeal:
        if (
            self._state != "validated"
            or self._latest_result is None
            or result is not self._latest_result
        ):
            raise OfficePrecommitStateError(
                "Office precommit result is stale or belongs to another session"
            )
        seal = result.commit_seal
        if seal is None:
            self._state = "aborted"
            raise OfficePrecommitRejectedError(
                "Office draft did not pass authoritative validation"
            )
        if (
            seal.relative_path != self._view.relative_path
            or seal.root_identity != self._view.staged_root_identity
            or seal.validation_generation != self._view.validation_generation
        ):
            self._state = "aborted"
            raise OfficePrecommitRejectedError(
                "Office draft seal is not bound to this transaction"
            )
        self._state = "committing"
        return seal

    def mark_committed(self, result: OfficeDraftValidationResult) -> None:
        if (
            self._state != "committing"
            or self._latest_result is None
            or result is not self._latest_result
        ):
            raise OfficePrecommitStateError(
                "Office precommit commit completion is out of order"
            )
        self._state = "committed"

    def abort(self) -> None:
        if self._state != "committed":
            self._state = "aborted"


class _EditValidationSession(_ValidationSessionBase):
    def __init__(
        self,
        *,
        service: OfficeDraftValidationService,
        request: OfficePrecommitRequest,
        view: WorkspaceOfficePrecommitView,
        baseline: OfficeDraftArtifact,
        plan: OfficeEditValidationPlan,
    ) -> None:
        super().__init__(service=service, request=request, view=view)
        self._baseline = baseline
        self._plan = plan

    def _compare(
        self,
        candidate: OfficeDraftArtifact,
    ) -> OfficeDraftValidationResult:
        return self._service.compare(
            baseline=self._baseline,
            candidate=candidate,
            allowed_changed_parts=self._plan.allowed_changed_parts,
            visual_policy=self._plan.visual_policy,
            expected_logical_unit_delta=self._plan.expected_logical_unit_delta,
        )


class _CreateValidationSession(_ValidationSessionBase):
    def __init__(
        self,
        *,
        service: OfficeDraftValidationService,
        request: OfficePrecommitRequest,
        view: WorkspaceOfficePrecommitView,
        golden: OfficeDraftArtifact,
        plan: OfficeCreateValidationPlan,
    ) -> None:
        super().__init__(service=service, request=request, view=view)
        self._golden = golden
        self._plan = plan

    def _compare(
        self,
        candidate: OfficeDraftArtifact,
    ) -> OfficeDraftValidationResult:
        return self._service.compare_with_golden(
            golden=self._golden,
            candidate=candidate,
            policy=self._plan.golden_policy,
            template_manifest=self._plan.template_manifest,
        )


class _StandaloneCreateValidationSession(_ValidationSessionBase):
    def __init__(
        self,
        *,
        service: OfficeDraftValidationService,
        request: OfficePrecommitRequest,
        view: WorkspaceOfficePrecommitView,
        plan: OfficeStandaloneCreateValidationPlan,
    ) -> None:
        super().__init__(service=service, request=request, view=view)
        self._plan = plan

    def _compare(
        self,
        candidate: OfficeDraftArtifact,
    ) -> OfficeDraftValidationResult:
        return self._service.validate_standalone_create(
            candidate=candidate,
            renderer_id=self._plan.renderer_id,
            renderer_version=self._plan.renderer_version,
            font_digest=self._plan.font_digest,
            parameters_version=self._plan.parameters_version,
            parameters_sha256=self._plan.parameters_sha256,
            visual_policy=self._plan.visual_policy,
        )


def _validate_request_view(
    request: OfficePrecommitRequest,
    view: WorkspaceOfficePrecommitView,
) -> None:
    if not isinstance(request, OfficePrecommitRequest) or not isinstance(
        view,
        WorkspaceOfficePrecommitView,
    ):
        raise OfficeValidationContractError(
            "Office precommit request or transaction view is invalid"
        )
    if (
        not isinstance(view.validation_generation, str)
        or len(view.validation_generation) != 64
        or any(
            character not in "0123456789abcdef"
            for character in view.validation_generation
        )
    ):
        raise OfficeValidationContractError(
            "Office precommit validation generation is invalid"
        )
    expected = (
        request.relative_path,
        f"office.{request.operation}",
        request.session_id,
        request.message_id,
        request.call_id,
        request.root_turn_id,
        request.turn_run_id,
        request.checkpoint_id,
        request.workspace_instance_id,
    )
    observed = (
        view.relative_path,
        view.operation,
        view.session_id,
        view.message_id,
        view.call_id,
        view.root_turn_id,
        view.turn_run_id,
        view.checkpoint_id,
        view.workspace_instance_id,
    )
    if observed != expected:
        raise OfficePrecommitRejectedError(
            "Office precommit runtime identity does not match its transaction"
        )
    if (
        view.visible_target != view.workspace_root / view.relative_path
        or view.staged_target != view.staged_root / view.relative_path
        or _directory_identity(view.workspace_root) != view.workspace_identity
        or _directory_identity(view.staged_root) != view.staged_root_identity
    ):
        raise OfficePrecommitRejectedError(
            "Office precommit transaction paths or roots changed"
        )


def _validate_visible_baseline(
    view: WorkspaceOfficePrecommitView,
    entry: WorkspaceEntry,
    artifact: OfficeDraftArtifact,
) -> None:
    if (
        artifact.boundary_root != view.workspace_root
        or artifact.source_path != view.visible_target
        or artifact.root_identity != view.workspace_identity
        or artifact.source_identity != view.baseline_identity
        or artifact.source_sha256 != entry.sha256
        or artifact.source_mode != entry.mode
        or artifact.source_size != entry.size
    ):
        raise OfficePrecommitRejectedError(
            "Office edit baseline differs from the transaction snapshot"
        )


def _validate_candidate_identity(
    request: OfficePrecommitRequest,
    view: WorkspaceOfficePrecommitView,
    candidate: OfficeDraftArtifact,
) -> None:
    if (
        candidate.boundary_root != view.staged_root
        or candidate.source_path != view.staged_target
        or candidate.root_identity != view.staged_root_identity
        or candidate.document_format != request.document_format
        or candidate.source_path.relative_to(candidate.boundary_root).as_posix()
        != view.relative_path
    ):
        raise OfficePrecommitRejectedError(
            "Office candidate differs from the armed transaction target"
        )


def _directory_identity(path: Path) -> tuple[int, int]:
    try:
        info = path.lstat()
    except OSError as exc:
        raise OfficePrecommitRejectedError(
            "Office precommit directory is unavailable"
        ) from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise OfficePrecommitRejectedError(
            "Office precommit directory is redirected"
        )
    return info.st_dev, info.st_ino


_RUNTIME_LOCK = threading.RLock()
_RUNTIME_COORDINATOR: OfficePrecommitCoordinator | None = None


def set_office_precommit_coordinator(
    coordinator: OfficePrecommitCoordinator | None,
) -> None:
    """Install an app-owned coordinator; ``None`` restores fail-closed mode."""

    if coordinator is not None and not isinstance(
        coordinator,
        OfficePrecommitCoordinator,
    ):
        raise TypeError("Office precommit coordinator is invalid")
    global _RUNTIME_COORDINATOR
    with _RUNTIME_LOCK:
        _RUNTIME_COORDINATOR = coordinator


def get_office_precommit_coordinator() -> OfficePrecommitCoordinator | None:
    """Return the app-owned coordinator without constructing a fallback."""

    with _RUNTIME_LOCK:
        return _RUNTIME_COORDINATOR


__all__ = [
    "DeterministicOfficePrecommitCoordinator",
    "OfficeCreateValidationPlan",
    "OfficeEditValidationPlan",
    "OfficeEditMutationIntent",
    "OfficeStandaloneCreatePolicyResolver",
    "OfficeStandaloneCreateValidationPlan",
    "OfficePrecommitCoordinator",
    "OfficePrecommitError",
    "OfficePrecommitPolicyResolver",
    "OfficePrecommitRejectedError",
    "OfficePrecommitRequest",
    "OfficePrecommitStateError",
    "OfficePrecommitUnavailableError",
    "OfficePrecommitValidationSession",
    "get_office_precommit_coordinator",
    "set_office_precommit_coordinator",
]
