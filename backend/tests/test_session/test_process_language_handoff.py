from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.schemas.chat import PromptRequest
from app.session.manager import create_message, create_part, create_session
from app.session.middleware import MiddlewareChain
from app.session.prompt import SessionPrompt
from app.streaming.manager import GenerationJob


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("language", "required", "excluded"),
    [
        ("zh", "用户可见的思考、进度和状态说明必须使用简体中文", "Keep all subsequent"),
        ("en", "Keep all subsequent user-visible reasoning", "必须使用简体中文"),
    ],
)
async def test_every_internal_step_ends_with_process_language_guard(
    session_factory,
    language: str,
    required: str,
    excluded: str,
) -> None:
    async with session_factory() as db:
        async with db.begin():
            session = await create_session(db, title="Tool language handoff")
            user = await create_message(
                db,
                session_id=session.id,
                data={"role": "user"},
            )
            await create_part(
                db,
                message_id=user.id,
                session_id=session.id,
                data={"type": "text", "text": "写一首关于盛夏的长诗"},
            )

    job = GenerationJob(
        "turn-run-language",
        session.id,
        language=language,  # type: ignore[arg-type]
    )
    prompt = SessionPrompt.__new__(SessionPrompt)
    prompt.session_factory = session_factory
    prompt.job = job
    prompt.request = PromptRequest(
        session_id=session.id,
        text="写一首关于盛夏的长诗",
        language=language,  # type: ignore[arg-type]
    )
    prompt.provider = SimpleNamespace(id="deepseek")
    prompt.model_id = "deepseek-v4-flash"
    prompt.model_info = None
    prompt.agent = SimpleNamespace(name="build")
    prompt.skip_user_message = False
    prompt.step = 2
    prompt.middleware_chain = MiddlewareChain()

    messages, _ = await SessionPrompt._prepare_step_messages(prompt)

    assert messages[-1]["role"] == "user"
    assert required in messages[-1]["content"]
    assert excluded not in messages[-1]["content"]
    assert (
        "不是真实用户消息" in messages[-1]["content"]
        if language == "zh"
        else "not a genuine user message" in messages[-1]["content"]
    )


@pytest.mark.asyncio
async def test_first_step_keeps_genuine_user_message_as_tail(session_factory) -> None:
    async with session_factory() as db:
        async with db.begin():
            session = await create_session(db, title="First step language")
            user = await create_message(
                db,
                session_id=session.id,
                data={"role": "user"},
            )
            await create_part(
                db,
                message_id=user.id,
                session_id=session.id,
                data={"type": "text", "text": "Keep the genuine request last"},
            )

    job = GenerationJob("first-step-run", session.id, language="zh")
    prompt = SessionPrompt.__new__(SessionPrompt)
    prompt.session_factory = session_factory
    prompt.job = job
    prompt.request = PromptRequest(
        session_id=session.id,
        text="Keep the genuine request last",
        language="zh",
    )
    prompt.provider = SimpleNamespace(id="deepseek")
    prompt.model_id = "deepseek-v4-flash"
    prompt.model_info = None
    prompt.agent = SimpleNamespace(name="build")
    prompt.skip_user_message = False
    prompt.step = 1
    prompt.middleware_chain = MiddlewareChain()

    messages, _ = await SessionPrompt._prepare_step_messages(prompt)

    assert messages[-1] == {
        "role": "user",
        "content": "Keep the genuine request last",
    }
