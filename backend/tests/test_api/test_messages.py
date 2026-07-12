"""Tests for message listing API endpoints."""

from __future__ import annotations

import pytest
from app.session.manager import create_message, create_part, create_session

pytestmark = pytest.mark.asyncio


class TestListMessages:
    async def test_empty_session(self, app_client, session_factory):
        async with session_factory() as db:
            async with db.begin():
                s = await create_session(db, title="Empty")
                sid = s.id
        resp = await app_client.get(f"/api/messages/{sid}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 0
        assert data["messages"] == []

    async def test_with_parts(self, app_client, session_factory):
        async with session_factory() as db:
            async with db.begin():
                s = await create_session(db, title="Chat")
                sid = s.id
                msg = await create_message(db, session_id=sid, data={"role": "user"})
                await create_part(db, message_id=msg.id, session_id=sid, data={"type": "text", "text": "Hi"})
        resp = await app_client.get(f"/api/messages/{sid}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["messages"][0]["data"]["role"] == "user"
        assert len(data["messages"][0]["parts"]) == 1

    async def test_negative_offset_latest(self, app_client, session_factory):
        async with session_factory() as db:
            async with db.begin():
                s = await create_session(db, title="Many")
                sid = s.id
                for i in range(5):
                    await create_message(db, session_id=sid, data={"role": "user", "i": i})
        resp = await app_client.get(f"/api/messages/{sid}", params={"offset": -1, "limit": 2})
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 5
        assert len(data["messages"]) == 2
        assert data["offset"] == 3

    async def test_explicit_offset(self, app_client, session_factory):
        async with session_factory() as db:
            async with db.begin():
                s = await create_session(db, title="Page")
                sid = s.id
                for i in range(3):
                    await create_message(db, session_id=sid, data={"role": "user"})
        resp = await app_client.get(f"/api/messages/{sid}", params={"offset": 0, "limit": 2})
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 3
        assert data["offset"] == 0
        assert len(data["messages"]) == 2


class TestGetMessage:
    async def test_existing(self, app_client, session_factory):
        async with session_factory() as db:
            async with db.begin():
                s = await create_session(db, title="T")
                sid = s.id
                msg = await create_message(db, session_id=sid, data={"role": "assistant"})
                mid = msg.id
        resp = await app_client.get(f"/api/messages/{sid}/{mid}")
        assert resp.status_code == 200
        assert resp.json()["data"]["role"] == "assistant"

    async def test_not_found(self, app_client, session_factory):
        async with session_factory() as db:
            async with db.begin():
                s = await create_session(db, title="T")
                sid = s.id
        resp = await app_client.get(f"/api/messages/{sid}/nonexistent")
        assert resp.status_code == 404

    async def test_wrong_session(self, app_client, session_factory):
        async with session_factory() as db:
            async with db.begin():
                s1 = await create_session(db, title="S1")
                s2 = await create_session(db, title="S2")
                msg = await create_message(db, session_id=s1.id, data={"role": "user"})
        resp = await app_client.get(f"/api/messages/{s2.id}/{msg.id}")
        assert resp.status_code == 404


class TestTurnIndex:
    async def test_indexes_and_directly_pages_a_200_turn_conversation(
        self, app_client, session_factory
    ):
        user_ids: list[str] = []
        async with session_factory() as db:
            async with db.begin():
                session = await create_session(db, title="Two hundred turns")
                sid = session.id
                for turn in range(1, 201):
                    user = await create_message(
                        db, session_id=sid, data={"role": "user"}
                    )
                    user_ids.append(user.id)
                    await create_part(
                        db,
                        message_id=user.id,
                        session_id=sid,
                        data={"type": "text", "text": f"Question {turn}"},
                    )
                    await create_message(
                        db, session_id=sid, data={"role": "assistant"}
                    )

        response = await app_client.get(f"/api/messages/{sid}/turn-index")
        assert response.status_code == 200
        payload = response.json()
        assert payload["total_messages"] == 400
        assert payload["total_turns"] == 200

        for turn_number, expected_offset in ((1, 0), (100, 198), (200, 398)):
            turn = payload["turns"][turn_number - 1]
            assert turn["ordinal"] == turn_number
            assert turn["message_id"] == user_ids[turn_number - 1]
            assert turn["message_offset"] == expected_offset
            page = await app_client.get(
                f"/api/messages/{sid}",
                params={"offset": turn["message_offset"], "limit": 1},
            )
            assert page.status_code == 200
            assert page.json()["messages"][0]["id"] == turn["message_id"]

    async def test_returns_visible_user_turns_with_stable_message_offsets(
        self, app_client, session_factory
    ):
        async with session_factory() as db:
            async with db.begin():
                session = await create_session(db, title="Outline")
                sid = session.id
                first = await create_message(
                    db, session_id=sid, data={"role": "user"}
                )
                await create_part(
                    db,
                    message_id=first.id,
                    session_id=sid,
                    data={
                        "type": "text",
                        "text": "  Explain\n  the   migration plan  ",
                    },
                )
                await create_part(
                    db,
                    message_id=first.id,
                    session_id=sid,
                    data={"type": "file", "name": "plan.md"},
                )
                await create_message(
                    db, session_id=sid, data={"role": "assistant"}
                )
                await create_message(
                    db,
                    session_id=sid,
                    data={"role": "user", "system": "continue"},
                )
                second = await create_message(
                    db, session_id=sid, data={"role": "user"}
                )
                await create_part(
                    db,
                    message_id=second.id,
                    session_id=sid,
                    data={"type": "file", "name": "evidence.pdf"},
                )

        response = await app_client.get(f"/api/messages/{sid}/turn-index")
        assert response.status_code == 200
        payload = response.json()
        assert payload["total_messages"] == 4
        assert payload["total_turns"] == 2
        assert [turn["message_id"] for turn in payload["turns"]] == [
            first.id,
            second.id,
        ]
        assert [turn["ordinal"] for turn in payload["turns"]] == [1, 2]
        assert [turn["message_offset"] for turn in payload["turns"]] == [0, 3]
        assert payload["turns"][0]["summary"] == "Explain the migration plan"
        assert payload["turns"][0]["attachment_names"] == ["plan.md"]
        assert payload["turns"][1]["summary"] == "evidence.pdf"

    async def test_truncates_summaries_without_returning_assistant_parts(
        self, app_client, session_factory
    ):
        async with session_factory() as db:
            async with db.begin():
                session = await create_session(db, title="Long outline")
                sid = session.id
                user = await create_message(db, session_id=sid, data={"role": "user"})
                await create_part(
                    db,
                    message_id=user.id,
                    session_id=sid,
                    data={"type": "text", "text": "x" * 300},
                )
                assistant = await create_message(
                    db, session_id=sid, data={"role": "assistant"}
                )
                await create_part(
                    db,
                    message_id=assistant.id,
                    session_id=sid,
                    data={"type": "tool", "state": {"output": "secret-output"}},
                )

        response = await app_client.get(f"/api/messages/{sid}/turn-index")
        assert response.status_code == 200
        body = response.text
        assert "secret-output" not in body
        summary = response.json()["turns"][0]["summary"]
        assert len(summary) == 160
        assert summary.endswith("…")
