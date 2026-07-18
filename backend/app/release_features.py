"""Server-side code-owned gates for high-privilege release features.

These constants are deliberately code-owned rather than environment-owned.  A
packaged release must not become remotely command-capable because a stale
``.env`` file still contains an old opt-in flag.  When a feature is ready for a
release, changing its availability requires an explicit reviewed source change
along with its security regression suite.
"""

from __future__ import annotations

from typing import Final


# Remote bearer access currently reaches the same Agent and host tools as the
# local desktop session.  Keep it unmountable in the v1.0 release scope.
REMOTE_ACCESS_RELEASED: Final = False

# Messaging channels still feed an unattended build Agent.  The individual
# channel modules remain available to developers, but the release server does
# not mount their management API or start their consumers.
MESSAGING_CHANNELS_RELEASED: Final = False

# Persistent Goal CRUD, commands, UI, migration, CAS, and recovery contracts
# have passed the stable local-desktop release suite.
GOALS_RELEASED: Final = True

# Autonomous continuation is a separate, stricter boundary.  It remains
# independently gateable, but its budget, permission, input-priority,
# pause/recovery, and no-progress safeguards are released for local desktop.
AUTONOMOUS_GOALS_RELEASED: Final = True

# v1.1 local-runtime surfaces are released as one dependency-complete graph.
# Runtime readiness remains a separate boundary: in particular, the explicit
# unsigned-degraded package contains no authoritative Office renderer, so
# preview may be approximate/unavailable and Office authoring still fails
# closed without an attested precommit coordinator.
V11_CHECKPOINTS_RELEASED: Final = True
V11_REWIND_RELEASED: Final = True
V11_HOOKS_RELEASED: Final = True
V11_ACP_RELEASED: Final = True
V11_WORKTREES_RELEASED: Final = True
V11_VALIDATION_AGENT_RELEASED: Final = True
V11_OFFICE_V2_RELEASED: Final = True
# User-imported Office templates have a separate Beta trust and confirmation
# boundary.  Enabling Office v1.1 authoring must never make this surface live.
V11_USER_OFFICE_TEMPLATES_BETA_RELEASED: Final = True
