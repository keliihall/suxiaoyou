"""Session manager tests (DB operations)."""

from datetime import datetime, timezone

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.idempotency_record import IdempotencyRecord
from app.models.session_goal import SessionGoal
from app.schemas.session import SessionResponse
from app.session.manager import (
    create_message,
    create_part,
    create_session,
    delete_messages_after,
    get_message_history_for_llm,
    get_messages,
    get_session,
    list_sessions,
    search_sessions,
    update_message_text,
    update_session_title,
)


class TestSessionManager:
    @pytest.mark.asyncio
    async def test_create_session(self, db: AsyncSession):
        session = await create_session(db, title="Test Session")
        assert session.id is not None
        assert session.title == "Test Session"

    @pytest.mark.asyncio
    async def test_get_session(self, db: AsyncSession):
        session = await create_session(db, title="Find Me")
        found = await get_session(db, session.id)
        assert found is not None
        assert found.title == "Find Me"

    @pytest.mark.asyncio
    async def test_get_nonexistent_session(self, db: AsyncSession):
        found = await get_session(db, "nonexistent-id")
        assert found is None

    @pytest.mark.asyncio
    async def test_list_sessions(self, db: AsyncSession):
        await create_session(db, title="S1")
        await create_session(db, title="S2")
        sessions = await list_sessions(db)
        assert len(sessions) >= 2

    @pytest.mark.asyncio
    async def test_list_and_search_sessions_include_goal_summary(
        self, db: AsyncSession
    ) -> None:
        session = await create_session(db, title="Goal summary contract")
        db.add(
            SessionGoal(
                session_id=session.id,
                objective="Ship the durable Goal summary",
                status="blocked",
                run_state="waiting_user",
                needs_review=True,
            )
        )
        await db.flush()
        db.expunge_all()

        listed = await list_sessions(db)
        listed_session = next(item for item in listed if item.id == session.id)
        listed_response = SessionResponse.model_validate(listed_session)
        assert listed_response.goal_status == "blocked"
        assert listed_response.goal_run_state == "waiting_user"
        assert listed_response.goal_needs_input is True
        assert (
            listed_response.goal_objective_preview
            == "Ship the durable Goal summary"
        )

        searched = await search_sessions(db, "Goal summary contract")
        searched_response = next(
            item.session for item in searched if item.session.id == session.id
        )
        assert searched_response.goal_status == "blocked"
        assert searched_response.goal_run_state == "waiting_user"
        assert searched_response.goal_needs_input is True
        assert (
            searched_response.goal_objective_preview
            == "Ship the durable Goal summary"
        )

    @pytest.mark.asyncio
    async def test_update_title(self, db: AsyncSession):
        session = await create_session(db, title="Old")
        await update_session_title(db, session.id, "New")
        updated = await get_session(db, session.id)
        assert updated.title == "New"


class TestMessageManager:
    @pytest.mark.asyncio
    async def test_history_edit_invalidates_all_bound_acp_replays_in_same_transaction(
        self,
        db: AsyncSession,
    ) -> None:
        session = await create_session(db, title="ACP history binding")
        target = await create_message(
            db,
            session_id=session.id,
            data={"role": "user", "acp_message_id": "acp-target"},
        )
        await create_part(
            db,
            message_id=target.id,
            session_id=session.id,
            data={"type": "text", "text": "original target"},
        )
        suffix = await create_message(
            db,
            session_id=session.id,
            data={"role": "user", "acp_message_id": "acp-suffix"},
        )
        await create_part(
            db,
            message_id=suffix.id,
            session_id=session.id,
            data={"type": "text", "text": "deleted suffix"},
        )
        target.time_created = datetime(2026, 7, 18, 8, 0, tzinfo=timezone.utc)
        suffix.time_created = datetime(2026, 7, 18, 8, 1, tzinfo=timezone.utc)
        session_id = session.id
        target_id = target.id
        scope = f"acp.prompt:{session_id}"
        for request_key, message_id in (
            ("acp-target", target.id),
            ("acp-suffix", suffix.id),
        ):
            db.add(
                IdempotencyRecord(
                    scope=scope,
                    request_key=request_key,
                    request_hash=f"hash-{request_key}",
                    status="completed",
                    response={
                        "userMessageId": message_id,
                        "staleReplayPayload": request_key,
                    },
                )
            )
        db.add(
            IdempotencyRecord(
                scope=scope,
                request_key="unbound-survivor",
                request_hash="hash-survivor",
                status="completed",
                response={"preserved": True},
            )
        )
        await db.flush()

        await update_message_text(db, target.id, "rewritten target")
        assert await delete_messages_after(db, session.id, target.id) == 1

        # Query before the fixture transaction commits: history and durable ACP
        # replay state must change atomically in the caller-owned transaction.
        db.expire_all()
        records = {
            record.request_key: record
            for record in (
                await db.execute(
                    select(IdempotencyRecord).where(IdempotencyRecord.scope == scope)
                )
            ).scalars()
        }
        for request_key in ("acp-target", "acp-suffix"):
            assert records[request_key].status == "interrupted"
            assert records[request_key].response == {}
            assert (
                records[request_key].error_message
                == "acp_prompt_history_changed"
            )
        assert records["unbound-survivor"].status == "completed"
        assert records["unbound-survivor"].response == {"preserved": True}

        messages = await get_messages(db, session_id)
        assert [message.id for message in messages] == [target_id]
        assert messages[0].parts[0].data == {
            "type": "text",
            "text": "rewritten target",
        }

    @pytest.mark.asyncio
    async def test_message_order_and_history_deletion_are_stable_for_tied_timestamps(
        self, db: AsyncSession
    ):
        session = await create_session(db, title="Stable message order")
        messages = [
            await create_message(db, session_id=session.id, data={"role": "user"})
            for _ in range(3)
        ]
        tied_time = datetime(2026, 7, 13, tzinfo=timezone.utc)
        for message in messages:
            message.time_created = tied_time
        await db.flush()

        ordered = await get_messages(db, session.id)
        assert [message.id for message in ordered] == sorted(
            message.id for message in messages
        )

        deleted = await delete_messages_after(db, session.id, ordered[0].id)
        assert deleted == 2
        remaining = await get_messages(db, session.id)
        assert [message.id for message in remaining] == [ordered[0].id]

    @pytest.mark.asyncio
    async def test_create_message_and_part(self, db: AsyncSession):
        session = await create_session(db, title="Msg Test")
        msg = await create_message(db, session_id=session.id, data={"role": "user"})
        assert msg.id is not None

        part = await create_part(
            db, message_id=msg.id, session_id=session.id,
            data={"type": "text", "text": "hello"},
        )
        assert part.id is not None

    @pytest.mark.asyncio
    async def test_get_messages_with_parts(self, db: AsyncSession):
        session = await create_session(db, title="Parts Test")

        msg = await create_message(db, session_id=session.id, data={"role": "user"})
        await create_part(
            db, message_id=msg.id, session_id=session.id,
            data={"type": "text", "text": "hello"},
        )
        await create_part(
            db, message_id=msg.id, session_id=session.id,
            data={"type": "text", "text": "world"},
        )

        messages = await get_messages(db, session.id)
        assert len(messages) == 1
        assert len(messages[0].parts) == 2

    @pytest.mark.asyncio
    async def test_message_history_for_llm(self, db: AsyncSession):
        session = await create_session(db, title="LLM History")

        # User message
        user_msg = await create_message(db, session_id=session.id, data={"role": "user"})
        await create_part(
            db, message_id=user_msg.id, session_id=session.id,
            data={"type": "text", "text": "What is 2+2?"},
        )

        # Assistant message
        asst_msg = await create_message(
            db,
            session_id=session.id,
            data={
                "role": "assistant",
                "provider_id": "deepseek",
                "model_id": "thinking-model",
            },
        )
        await create_part(
            db, message_id=asst_msg.id, session_id=session.id,
            data={"type": "text", "text": "4"},
        )

        history = await get_message_history_for_llm(db, session.id)
        assert len(history) == 2
        assert history[0]["role"] == "user"
        assert history[0]["content"] == "What is 2+2?"
        assert history[1]["role"] == "assistant"
        assert history[1]["content"] == "4"

    @pytest.mark.asyncio
    async def test_history_reasoning_echo_provider_matrix(
        self, db: AsyncSession,
    ):
        """Issue #126: thinking-mode providers using DeepSeek's
        `reasoning_content` convention 400 on multi-turn follow-ups unless the
        prior assistant turn echoes its reasoning. The exceptions are:

          * providers with their own reasoning protocol
            (openrouter / anthropic / google / openai / azure)
          * the legacy `deepseek-reasoner` (R1) model, which rejects the field
            on input.
        """
        session = await create_session(db, title="Thinking Echo")

        user_msg = await create_message(db, session_id=session.id, data={"role": "user"})
        await create_part(
            db, message_id=user_msg.id, session_id=session.id,
            data={"type": "text", "text": "What is 2+2?"},
        )

        asst_msg = await create_message(db, session_id=session.id, data={"role": "assistant"})
        await create_part(
            db, message_id=asst_msg.id, session_id=session.id,
            data={"type": "reasoning", "text": "User wants arithmetic. 2+2=4."},
        )
        await create_part(
            db, message_id=asst_msg.id, session_id=session.id,
            data={"type": "text", "text": "4"},
        )

        expected_reasoning = "User wants arithmetic. 2+2=4."

        # Every openai-compat provider that surfaces reasoning_content must
        # receive the echo. Default behavior — no enumeration needed except
        # for documentation: catalog providers + ollama / rapid-mlx / BYOK.
        echo_providers = (
            "deepseek", "kimi", "qwen", "zhipu",
            "groq", "mistral", "xai", "together", "deepinfra",
            "cerebras", "cohere", "perplexity", "fireworks",
            "minimax", "siliconflow", "xiaomi",
            "ollama", "rapid-mlx",
            "some-byok-id",  # GenericOpenAIProvider with a custom id
        )
        for provider_id in echo_providers:
            asst_msg.data = {
                "role": "assistant",
                "provider_id": provider_id,
                "model_id": "thinking-model",
                "process_language": "zh",
            }
            await db.flush()
            history = await get_message_history_for_llm(
                db,
                session.id,
                provider_id=provider_id,
                model_id="thinking-model",
                process_language="zh",
            )
            assert history[1]["role"] == "assistant"
            assert history[1].get("reasoning_content") == expected_reasoning, (
                f"{provider_id} should echo reasoning_content"
            )

        # Providers with their own reasoning protocol — never echo.
        skip_providers = (
            "openrouter", "anthropic", "google",
            "openai", "openai-subscription", "azure",
            None,  # no provider hint (compaction / workspace memory callers)
        )
        for provider_id in skip_providers:
            history = await get_message_history_for_llm(
                db,
                session.id,
                provider_id=provider_id,
                model_id="thinking-model",
                process_language="zh",
            )
            assert "reasoning_content" not in history[1], (
                f"{provider_id} must not receive reasoning_content"
            )

        # Legacy deepseek-reasoner (R1) actively 400s when reasoning_content
        # is included on input — strip even though the provider is deepseek.
        asst_msg.data = {
            "role": "assistant",
            "provider_id": "deepseek",
            "model_id": "deepseek-reasoner",
            "process_language": "zh",
        }
        await db.flush()
        history = await get_message_history_for_llm(
            db,
            session.id,
            provider_id="deepseek",
            model_id="deepseek-reasoner",
            process_language="zh",
        )
        assert "reasoning_content" not in history[1]

        # Match is exact, not prefix: a hypothetical future model whose name
        # starts with `deepseek-reasoner-` is not assumed to share R1's
        # rejection rule, so the echo still applies.
        asst_msg.data = {
            "role": "assistant",
            "provider_id": "deepseek",
            "model_id": "deepseek-reasoner-v2",
            "process_language": "zh",
        }
        await db.flush()
        history = await get_message_history_for_llm(
            db,
            session.id,
            provider_id="deepseek",
            model_id="deepseek-reasoner-v2",
            process_language="zh",
        )
        assert history[1].get("reasoning_content") == expected_reasoning

        # Provenance matching is intentionally fail-closed when the caller
        # cannot identify the exact active model.
        history = await get_message_history_for_llm(
            db, session.id, provider_id="deepseek", process_language="zh",
        )
        assert "reasoning_content" not in history[1]

    @pytest.mark.asyncio
    async def test_history_reasoning_only_assistant_turn_preserved(
        self, db: AsyncSession,
    ):
        """An assistant turn with only reasoning (no text, no tool_calls) must
        survive in history when echo is on, otherwise the assistant slot goes
        missing from the alternating sequence.
        """
        session = await create_session(db, title="Reasoning-only turn")

        user_msg = await create_message(db, session_id=session.id, data={"role": "user"})
        await create_part(
            db, message_id=user_msg.id, session_id=session.id,
            data={"type": "text", "text": "Continue thinking."},
        )

        asst_msg = await create_message(
            db,
            session_id=session.id,
            data={
                "role": "assistant",
                "provider_id": "deepseek",
                "model_id": "deepseek-v4-flash",
                "process_language": "zh",
            },
        )
        await create_part(
            db, message_id=asst_msg.id, session_id=session.id,
            data={"type": "reasoning", "text": "Still working it out."},
        )

        echoed = await get_message_history_for_llm(
            db,
            session.id,
            provider_id="deepseek",
            model_id="deepseek-v4-flash",
            process_language="zh",
        )
        assert echoed[-1]["role"] == "assistant"
        assert echoed[-1]["content"] == ""
        assert echoed[-1]["reasoning_content"] == "Still working it out."

        # When echo is off the orphan turn is collapsed (no value in keeping
        # an empty assistant message that won't be sent).
        skipped = await get_message_history_for_llm(db, session.id)
        assert all(m["role"] == "user" for m in skipped)

    @pytest.mark.asyncio
    async def test_history_reasoning_content_is_trimmed(self, db: AsyncSession):
        """Reasoning blocks can be tens of thousands of chars; per-message
        trimming caps each turn so a single thinking-heavy reply cannot
        consume the entire request budget.
        """
        session = await create_session(db, title="Huge reasoning")

        user_msg = await create_message(db, session_id=session.id, data={"role": "user"})
        await create_part(
            db, message_id=user_msg.id, session_id=session.id,
            data={"type": "text", "text": "ping"},
        )

        huge = "x" * 200_000
        asst_msg = await create_message(
            db,
            session_id=session.id,
            data={
                "role": "assistant",
                "provider_id": "deepseek",
                "model_id": "deepseek-v4-flash",
                "process_language": "zh",
            },
        )
        await create_part(
            db, message_id=asst_msg.id, session_id=session.id,
            data={"type": "reasoning", "text": huge},
        )
        await create_part(
            db, message_id=asst_msg.id, session_id=session.id,
            data={"type": "text", "text": "pong"},
        )

        history = await get_message_history_for_llm(
            db,
            session.id,
            provider_id="deepseek",
            model_id="deepseek-v4-flash",
            process_language="zh",
        )
        reasoning = history[1]["reasoning_content"]
        assert len(reasoning) < len(huge)
        assert "[思考内容已为上下文截断" in reasoning
        assert "reasoning truncated for context" not in reasoning

    @pytest.mark.asyncio
    async def test_reasoning_lineage_resets_across_model_switches(
        self, db: AsyncSession,
    ):
        """A -> B -> A must not resurrect A's pre-switch reasoning.

        Tool frames before the active lineage are removed together so a
        thinking-mode provider never receives tool_calls without their required
        reasoning_content. Plain assistant text remains as useful context.
        """

        session = await create_session(db, title="Reasoning lineage")

        async def add_user(text: str) -> None:
            message = await create_message(
                db, session_id=session.id, data={"role": "user"},
            )
            await create_part(
                db,
                message_id=message.id,
                session_id=session.id,
                data={"type": "text", "text": text},
            )

        async def add_assistant(
            provider_id: str,
            model_id: str,
            label: str,
        ) -> None:
            message = await create_message(
                db,
                session_id=session.id,
                data={
                    "role": "assistant",
                    "provider_id": provider_id,
                    "model_id": model_id,
                    "process_language": "zh",
                },
            )
            await create_part(
                db,
                message_id=message.id,
                session_id=session.id,
                data={"type": "reasoning", "text": f"reasoning-{label}"},
            )
            await create_part(
                db,
                message_id=message.id,
                session_id=session.id,
                data={"type": "text", "text": f"answer-{label}"},
            )
            await create_part(
                db,
                message_id=message.id,
                session_id=session.id,
                data={
                    "type": "tool",
                    "tool": "read",
                    "call_id": f"call-{label}",
                    "state": {
                        "status": "completed",
                        "input": {"file_path": f"{label}.txt"},
                        "output": f"output-{label}",
                    },
                },
            )

        await add_user("first")
        await add_assistant("deepseek", "deepseek-v4-flash", "old-a")
        await add_user("switch to B")
        await add_assistant("kimi", "kimi-k3", "b")
        await add_user("switch back to A")
        await add_assistant("deepseek", "deepseek-v4-flash", "new-a")
        await add_user("continue")

        history = await get_message_history_for_llm(
            db,
            session.id,
            provider_id="deepseek",
            model_id="deepseek-v4-flash",
            process_language="zh",
        )
        assistants = {
            message.get("content"): message
            for message in history
            if message.get("role") == "assistant"
        }

        assert set(assistants) == {"answer-old-a", "answer-b", "answer-new-a"}
        assert "reasoning_content" not in assistants["answer-old-a"]
        assert "tool_calls" not in assistants["answer-old-a"]
        assert "reasoning_content" not in assistants["answer-b"]
        assert "tool_calls" not in assistants["answer-b"]
        assert assistants["answer-new-a"]["reasoning_content"] == "reasoning-new-a"
        assert assistants["answer-new-a"]["tool_calls"][0]["id"] == "call-new-a"
        assert [
            message["tool_call_id"]
            for message in history
            if message.get("role") == "tool"
        ] == ["call-new-a"]

    @pytest.mark.asyncio
    async def test_reasoning_lineage_resets_on_process_language_change(
        self, db: AsyncSession,
    ):
        session = await create_session(db, title="Reasoning language lineage")

        async def add_assistant(language: str | None, label: str) -> None:
            data = {
                "role": "assistant",
                "provider_id": "deepseek",
                "model_id": "deepseek-v4-flash",
            }
            if language is not None:
                data["process_language"] = language
            message = await create_message(
                db, session_id=session.id, data=data,
            )
            await create_part(
                db,
                message_id=message.id,
                session_id=session.id,
                data={"type": "reasoning", "text": f"reasoning-{label}"},
            )
            await create_part(
                db,
                message_id=message.id,
                session_id=session.id,
                data={"type": "text", "text": f"answer-{label}"},
            )

        # Existing pre-upgrade records have no process-language provenance and
        # must reset rather than perpetuate already polluted reasoning.
        await add_assistant(None, "legacy")
        zh_before_new = await get_message_history_for_llm(
            db,
            session.id,
            provider_id="deepseek",
            model_id="deepseek-v4-flash",
            process_language="zh",
        )
        assert zh_before_new == [
            {"role": "assistant", "content": "answer-legacy"},
        ]

        await add_assistant("zh", "zh")
        zh_history = await get_message_history_for_llm(
            db,
            session.id,
            provider_id="deepseek",
            model_id="deepseek-v4-flash",
            process_language="zh",
        )
        assert "reasoning_content" not in zh_history[0]
        assert zh_history[1]["reasoning_content"] == "reasoning-zh"

        # Switching the UI/process locale starts another clean lineage even
        # though provider and model are unchanged.
        en_before_new = await get_message_history_for_llm(
            db,
            session.id,
            provider_id="deepseek",
            model_id="deepseek-v4-flash",
            process_language="en",
        )
        assert all("reasoning_content" not in message for message in en_before_new)

        await add_assistant("en", "en")
        en_history = await get_message_history_for_llm(
            db,
            session.id,
            provider_id="deepseek",
            model_id="deepseek-v4-flash",
            process_language="en",
        )
        assert all(
            "reasoning_content" not in message
            for message in en_history[:-1]
        )
        assert en_history[-1]["reasoning_content"] == "reasoning-en"

    @pytest.mark.asyncio
    async def test_reasoning_lineage_is_scoped_to_current_turn_run(
        self, db: AsyncSession,
    ) -> None:
        session = await create_session(db, title="Reasoning run lineage")

        async def add_tool_step(run_id: str, label: str) -> None:
            message = await create_message(
                db,
                session_id=session.id,
                data={
                    "role": "assistant",
                    "provider_id": "deepseek",
                    "model_id": "deepseek-v4-flash",
                    "process_language": "zh",
                    "turn_run_id": run_id,
                },
            )
            await create_part(
                db,
                message_id=message.id,
                session_id=session.id,
                data={"type": "reasoning", "text": f"reasoning-{label}"},
            )
            await create_part(
                db,
                message_id=message.id,
                session_id=session.id,
                data={
                    "type": "tool",
                    "tool": "artifact",
                    "call_id": f"call-{label}",
                    "state": {
                        "status": "completed",
                        "input": {},
                        "output": f"output-{label}",
                    },
                },
            )

        await add_tool_step("goal-run-old", "old")
        await add_tool_step("goal-run-current", "current")

        history = await get_message_history_for_llm(
            db,
            session.id,
            provider_id="deepseek",
            model_id="deepseek-v4-flash",
            process_language="zh",
            turn_run_id="goal-run-current",
        )

        assistant_frames = [
            message for message in history if message.get("role") == "assistant"
        ]
        assert len(assistant_frames) == 1
        assert assistant_frames[0]["reasoning_content"] == "reasoning-current"
        assert assistant_frames[0]["tool_calls"][0]["id"] == "call-current"
        assert [
            message["tool_call_id"]
            for message in history
            if message.get("role") == "tool"
        ] == ["call-current"]

    @pytest.mark.asyncio
    async def test_reasoning_without_provenance_fails_closed(
        self, db: AsyncSession,
    ):
        session = await create_session(db, title="Legacy reasoning")
        assistant = await create_message(
            db, session_id=session.id, data={"role": "assistant"},
        )
        await create_part(
            db,
            message_id=assistant.id,
            session_id=session.id,
            data={"type": "reasoning", "text": "unknown source"},
        )
        await create_part(
            db,
            message_id=assistant.id,
            session_id=session.id,
            data={"type": "text", "text": "legacy answer"},
        )

        history = await get_message_history_for_llm(
            db,
            session.id,
            provider_id="deepseek",
            model_id="deepseek-v4-flash",
            process_language="zh",
        )

        assert history == [{"role": "assistant", "content": "legacy answer"}]

    @pytest.mark.asyncio
    async def test_history_with_tool_calls(self, db: AsyncSession):
        session = await create_session(db, title="Tool History")

        # User message
        user_msg = await create_message(db, session_id=session.id, data={"role": "user"})
        await create_part(
            db, message_id=user_msg.id, session_id=session.id,
            data={"type": "text", "text": "Read test.py"},
        )

        # Assistant with tool call
        asst_msg = await create_message(db, session_id=session.id, data={"role": "assistant"})
        await create_part(
            db, message_id=asst_msg.id, session_id=session.id,
            data={"type": "text", "text": "Let me read that file."},
        )
        await create_part(
            db, message_id=asst_msg.id, session_id=session.id,
            data={
                "type": "tool", "tool": "read", "call_id": "call_1",
                "state": {
                    "status": "completed",
                    "input": {"file_path": "test.py"},
                    "output": "print('hello')",
                },
            },
        )

        history = await get_message_history_for_llm(db, session.id)
        # Should be: user, assistant (with tool_calls), tool result
        assert len(history) == 3
        assert history[0]["role"] == "user"
        assert history[1]["role"] == "assistant"
        assert "tool_calls" in history[1]
        assert history[2]["role"] == "tool"
        assert history[2]["content"] == "print('hello')"
