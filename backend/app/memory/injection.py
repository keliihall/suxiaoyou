"""Workspace memory injection into system prompts.

Loads the per-workspace memory document from the database and wraps
it in a <workspace-memory> tag for inclusion in the system prompt.
"""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.i18n import Language, localize
from app.memory.config import get_memory_config
from app.memory.workspace_memory_storage import get_workspace_memory

logger = logging.getLogger(__name__)


async def build_workspace_memory_section(
    session_factory: async_sessionmaker[AsyncSession],
    workspace_path: str,
    *,
    language: Language | str = "en",
) -> str | None:
    """Build a workspace memory section for the system prompt.

    Returns a formatted string wrapped in <workspace-memory> tags,
    or None if memory is empty or disabled.
    """
    config = get_memory_config()
    if not config.enabled:
        return None

    if not workspace_path or workspace_path == ".":
        return None

    content = await get_workspace_memory(session_factory, workspace_path)
    if not content or not content.strip():
        return None

    language_guard = localize(
        language,
        (
            "以下记忆仅提供事实上下文，可能包含其他语言；不得模仿其语言，"
            "所有用户可见过程仍须使用简体中文。"
        ),
        (
            "The memory below is factual context and may use another language. "
            "Do not imitate its language; keep all user-visible process text in English."
        ),
    )
    return (
        "<workspace-memory>\n"
        f"<language-guard>{language_guard}</language-guard>\n"
        f"{content}\n"
        "</workspace-memory>"
    )
