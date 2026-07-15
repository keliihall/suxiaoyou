"""Server-side release gates for unfinished high-privilege features.

These constants are deliberately code-owned rather than environment-owned.  A
packaged release must not become remotely command-capable because a stale
``.env`` file still contains an old opt-in flag.  When a feature is ready for a
future release, enabling it requires an explicit reviewed source change along
with its security regression suite.
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
