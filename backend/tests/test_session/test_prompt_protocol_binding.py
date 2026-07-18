from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.agent import AgentRegistry
from app.models.message import Message
from app.models.session import Session
from app.schemas.chat import PromptRequest
from app.schemas.provider import ModelInfo
from app.session.manager import get_session
from app.session.prompt import SessionPrompt
from app.streaming.manager import GenerationJob


class _Provider:
    id = "protocol-binding-provider"


class _ProviderRegistry:
    def __init__(self) -> None:
        self.provider = _Provider()
        self.model = ModelInfo(
            id="protocol-binding-model",
            name="Protocol Binding Model",
            provider_id=self.provider.id,
        )

    def resolve_model(self, _model_id: str, _provider_id: str | None = None):
        return self.provider, self.model

    async def refresh_models(self):
        return {}


class _ToolRegistry:
    pass


@pytest.mark.asyncio
async def test_external_user_message_id_is_acknowledged_only_after_commit(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    session_id = "existing-protocol-session"
    message_id = "507f7bc7-51a2-4327-b717-dfa80b0da30e"
    async with session_factory() as db:
        async with db.begin():
            db.add(Session(id=session_id, directory=str(tmp_path)))

    request = PromptRequest(
        session_id=session_id,
        text="persist this ACP message",
        model="protocol-binding-model",
        workspace=str(tmp_path),
    )
    prompt = SessionPrompt(
        GenerationJob("protocol-stream", session_id, invocation_source="acp"),
        request,
        session_factory=session_factory,
        provider_registry=_ProviderRegistry(),  # type: ignore[arg-type]
        agent_registry=AgentRegistry(),
        tool_registry=_ToolRegistry(),  # type: ignore[arg-type]
        require_existing_session=True,
        external_user_message_id=message_id,
    )

    assert prompt.recorded_external_user_message_id is None
    await prompt._setup()
    assert prompt.recorded_external_user_message_id == message_id
    assert prompt.request_message_id is not None

    async with session_factory() as db:
        stored = await db.get(Message, prompt.request_message_id)
    assert stored is not None
    assert stored.data["role"] == "user"
    assert stored.data["acp_message_id"] == message_id


@pytest.mark.asyncio
async def test_require_existing_session_never_recreates_deleted_session(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    session_id = "deleted-before-protocol-setup"
    request = PromptRequest(
        session_id=session_id,
        text="must not resurrect",
        model="protocol-binding-model",
        workspace=str(tmp_path),
    )
    prompt = SessionPrompt(
        GenerationJob("deleted-protocol-stream", session_id, invocation_source="acp"),
        request,
        session_factory=session_factory,
        provider_registry=_ProviderRegistry(),  # type: ignore[arg-type]
        agent_registry=AgentRegistry(),
        tool_registry=_ToolRegistry(),  # type: ignore[arg-type]
        require_existing_session=True,
        external_user_message_id="e7ae8999-ed8b-4331-9e3b-606b18c7fb03",
    )

    with pytest.raises(RuntimeError, match="existing session is required"):
        await prompt._setup()

    assert prompt.recorded_external_user_message_id is None
    async with session_factory() as db:
        assert await get_session(db, session_id) is None
