"""v1.1 Hook contracts, strict project loading, and runtime adapter.

The release gate remains closed and the package is not imported by the session
loop or public API.  ``HookRuntime`` is an explicit adapter for a later reviewed
integration rather than an implicit registration side effect.
"""

from app.hooks.config import (
    MAX_PROJECT_HOOK_CONFIG_BYTES,
    PROJECT_HOOK_CONFIG_RELATIVE_PATH,
    ProjectHookConfigError,
    ProjectHookConfigSecurityError,
    ProjectHookConfigV1,
    load_project_hook_config,
    register_project_hook_config,
)
from app.hooks.dispatcher import (
    HookDispatchResult,
    HookDispatchState,
    HookDispatcher,
    HookExecutionRecord,
)
from app.hooks.models import (
    BuiltinHookDeclaration,
    HookCommandDeclaration,
    HookDecision,
    HookDecisionKind,
    HookEvent,
    HookEventName,
    HookFailurePolicy,
    HookProtocolError,
    HookSource,
    combine_pre_tool_decisions,
)
from app.hooks.registry import (
    BuiltinHook,
    CommandHook,
    HookRegistry,
    refresh_command_hook,
)
from app.hooks.runner import HookCommandRunner, HookRunResult, HookRunStatus
from app.hooks.runtime import (
    HOOK_LIFECYCLE_EVENT_TYPES,
    HookApprovalError,
    HookApprovalMismatch,
    HookApprovalRequest,
    HookApprovalStale,
    HookApprovalUnavailable,
    HookDispatchAuditSummary,
    HookExecutionAudit,
    HookRuntime,
    HookRuntimeAdapter,
    HookRuntimeError,
    HookRuntimeResult,
    hook_event_from_lifecycle,
)
from app.hooks.trust import (
    HookApprovalRequired,
    HookTrustStore,
    HookTrustStoreError,
)

__all__ = [
    "BuiltinHook",
    "BuiltinHookDeclaration",
    "CommandHook",
    "HOOK_LIFECYCLE_EVENT_TYPES",
    "HookApprovalError",
    "HookApprovalMismatch",
    "HookApprovalRequest",
    "HookApprovalRequired",
    "HookApprovalStale",
    "HookApprovalUnavailable",
    "HookCommandDeclaration",
    "HookCommandRunner",
    "HookDecision",
    "HookDecisionKind",
    "HookDispatchResult",
    "HookDispatchAuditSummary",
    "HookDispatchState",
    "HookDispatcher",
    "HookEvent",
    "HookEventName",
    "HookExecutionRecord",
    "HookExecutionAudit",
    "HookFailurePolicy",
    "HookProtocolError",
    "HookRegistry",
    "HookRunResult",
    "HookRunStatus",
    "HookRuntime",
    "HookRuntimeAdapter",
    "HookRuntimeError",
    "HookRuntimeResult",
    "HookSource",
    "HookTrustStore",
    "HookTrustStoreError",
    "MAX_PROJECT_HOOK_CONFIG_BYTES",
    "PROJECT_HOOK_CONFIG_RELATIVE_PATH",
    "ProjectHookConfigError",
    "ProjectHookConfigSecurityError",
    "ProjectHookConfigV1",
    "combine_pre_tool_decisions",
    "hook_event_from_lifecycle",
    "load_project_hook_config",
    "refresh_command_hook",
    "register_project_hook_config",
]
