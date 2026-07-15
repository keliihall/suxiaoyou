"""Tests for session CRUD API endpoints."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.api.sessions import _extract_file_paths_from_messages, _list_session_todos
from app.errors import InternalError
from app.models.session_goal import SessionGoal
from app.models.todo import Todo

pytestmark = pytest.mark.asyncio


class TestListSessions:
    async def test_empty(self, app_client):
        resp = await app_client.get("/api/sessions")
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_returns_created(self, app_client):
        await app_client.post("/api/sessions", json={"title": "First"})
        await app_client.post("/api/sessions", json={"title": "Second"})
        resp = await app_client.get("/api/sessions")
        assert resp.status_code == 200
        assert len(resp.json()) == 2

    async def test_pagination(self, app_client):
        for i in range(5):
            await app_client.post("/api/sessions", json={"title": f"S{i}"})
        resp = await app_client.get("/api/sessions", params={"limit": 2, "offset": 0})
        assert resp.status_code == 200
        assert len(resp.json()) == 2

    async def test_project_id_filter(self, app_client):
        await app_client.post("/api/sessions", json={"title": "A", "project_id": "p1"})
        await app_client.post("/api/sessions", json={"title": "B", "project_id": "p2"})
        resp = await app_client.get("/api/sessions", params={"project_id": "p1"})
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["project_id"] == "p1"

    async def test_list_and_search_expose_goal_summary(
        self, app_client, session_factory
    ) -> None:
        created = await app_client.post(
            "/api/sessions", json={"title": "Goal API summary"}
        )
        session_id = created.json()["id"]
        async with session_factory() as db:
            async with db.begin():
                db.add(
                    SessionGoal(
                        session_id=session_id,
                        objective="Finish the stable Goal release",
                        status="blocked",
                        run_state="waiting_user",
                        needs_review=True,
                    )
                )

        listed = await app_client.get("/api/sessions")
        listed_item = next(
            item for item in listed.json() if item["id"] == session_id
        )
        assert listed_item["goal_status"] == "blocked"
        assert listed_item["goal_run_state"] == "waiting_user"
        assert listed_item["goal_needs_input"] is True
        assert (
            listed_item["goal_objective_preview"]
            == "Finish the stable Goal release"
        )

        searched = await app_client.get(
            "/api/sessions/search", params={"q": "Goal API summary"}
        )
        searched_item = next(
            item["session"]
            for item in searched.json()
            if item["session"]["id"] == session_id
        )
        assert searched_item["goal_status"] == "blocked"
        assert searched_item["goal_run_state"] == "waiting_user"
        assert searched_item["goal_needs_input"] is True
        assert (
            searched_item["goal_objective_preview"]
            == "Finish the stable Goal release"
        )


class TestCreateSession:
    async def test_success(self, app_client):
        resp = await app_client.post("/api/sessions", json={"title": "Test"})
        assert resp.status_code == 201
        data = resp.json()
        assert data["title"] == "Test"
        assert "id" in data

    async def test_defaults(self, app_client):
        resp = await app_client.post("/api/sessions", json={})
        assert resp.status_code == 201
        assert resp.json()["title"] == "New Session"

    async def test_with_directory(self, app_client):
        resp = await app_client.post(
            "/api/sessions",
            json={"title": "X", "project_id": "proj", "directory": "/tmp/test"},
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["project_id"] == "proj"
        assert data["directory"] == "/tmp/test"


class TestGetSession:
    async def test_existing(self, app_client):
        create = await app_client.post("/api/sessions", json={"title": "Get me"})
        sid = create.json()["id"]
        resp = await app_client.get(f"/api/sessions/{sid}")
        assert resp.status_code == 200
        assert resp.json()["title"] == "Get me"

    async def test_not_found(self, app_client):
        resp = await app_client.get("/api/sessions/nonexistent")
        assert resp.status_code == 404

    async def test_model_fields_present_and_null_for_fresh_session(self, app_client):
        """Per-session model memory: the model_id/provider_id fields are part
        of the session response, and null until a prompt sets them."""
        create = await app_client.post("/api/sessions", json={"title": "M"})
        body = create.json()
        assert body["model_id"] is None
        assert body["provider_id"] is None

        resp = await app_client.get(f"/api/sessions/{body['id']}")
        got = resp.json()
        assert "model_id" in got and got["model_id"] is None
        assert "provider_id" in got and got["provider_id"] is None


class TestSessionTodos:
    async def test_reload_groups_goal_and_ordinary_todos_without_mixing(
        self,
        app_client,
        session_factory,
    ) -> None:
        created = await app_client.post(
            "/api/sessions",
            json={"title": "Scoped todos"},
        )
        session_id = created.json()["id"]
        async with session_factory() as db:
            async with db.begin():
                goal = SessionGoal(
                    session_id=session_id,
                    objective="Keep working",
                    status="budget_limited",
                )
                db.add(goal)
                await db.flush()
                db.add_all([
                    Todo(
                        id="ordinary-todo",
                        session_id=session_id,
                        content="Ordinary follow-up",
                        status="pending",
                        position=0,
                    ),
                    Todo(
                        id="goal-todo",
                        session_id=session_id,
                        goal_id=goal.id,
                        content="Goal work",
                        status="in_progress",
                        active_form="Working on Goal",
                        position=0,
                    ),
                ])

        response = await app_client.get(f"/api/sessions/{session_id}/todos")

        assert response.status_code == 200
        payload = response.json()
        assert payload["scope"] == "goal"
        assert payload["goal_id"] == goal.id
        assert payload["goal_status"] == "budget_limited"
        assert payload["todos"] == [{
            "content": "Goal work",
            "status": "in_progress",
            "activeForm": "Working on Goal",
        }]
        assert payload["groups"] == {
            "session": [{
                "content": "Ordinary follow-up",
                "status": "pending",
                "activeForm": "",
            }],
            "goal": payload["todos"],
        }

    async def test_reload_without_goal_preserves_ordinary_todos(
        self,
        app_client,
        session_factory,
    ) -> None:
        created = await app_client.post(
            "/api/sessions",
            json={"title": "Ordinary todos"},
        )
        session_id = created.json()["id"]
        async with session_factory() as db:
            async with db.begin():
                db.add(Todo(
                    id="ordinary-only",
                    session_id=session_id,
                    content="Ordinary task",
                    status="completed",
                ))

        payload = (await app_client.get(
            f"/api/sessions/{session_id}/todos",
        )).json()

        assert payload["scope"] == "session"
        assert payload["goal_id"] is None
        assert payload["todos"] == payload["groups"]["session"]
        assert payload["groups"]["goal"] == []

    async def test_reload_fails_closed_when_storage_is_unavailable(
        self,
        monkeypatch,
    ) -> None:
        def unavailable_factory():
            raise RuntimeError("Database not initialized")

        monkeypatch.setattr(
            "app.api.sessions.get_session_factory",
            unavailable_factory,
        )

        with pytest.raises(InternalError, match="Todo storage is unavailable"):
            await _list_session_todos(
                "session",
                SimpleNamespace(),  # type: ignore[arg-type]
            )


class TestUpdateSession:
    async def test_update_title(self, app_client):
        create = await app_client.post("/api/sessions", json={"title": "Old"})
        sid = create.json()["id"]
        resp = await app_client.patch(f"/api/sessions/{sid}", json={"title": "New"})
        assert resp.status_code == 200
        assert resp.json()["title"] == "New"

    async def test_not_found(self, app_client):
        resp = await app_client.patch("/api/sessions/nope", json={"title": "X"})
        assert resp.status_code == 404

    async def test_update_directory(self, app_client):
        create = await app_client.post("/api/sessions", json={})
        sid = create.json()["id"]
        resp = await app_client.patch(f"/api/sessions/{sid}", json={"directory": "/new"})
        assert resp.status_code == 200
        assert resp.json()["directory"] == "/new"

    async def test_permission_state_is_not_publicly_writable(self, app_client):
        create = await app_client.post("/api/sessions", json={})
        sid = create.json()["id"]

        resp = await app_client.patch(
            f"/api/sessions/{sid}",
            json={
                "permission": [
                    {"action": "allow", "permission": "bash", "pattern": "*"},
                ],
            },
        )

        assert resp.status_code == 422
        assert (await app_client.get(f"/api/sessions/{sid}")).json()["permission"] is None


class TestDeleteSession:
    async def test_success(self, app_client):
        create = await app_client.post("/api/sessions", json={"title": "Del"})
        sid = create.json()["id"]
        resp = await app_client.delete(f"/api/sessions/{sid}")
        assert resp.status_code == 200
        assert resp.json()["deleted"] is True
        assert (await app_client.get(f"/api/sessions/{sid}")).status_code == 404

    async def test_not_found(self, app_client):
        resp = await app_client.delete("/api/sessions/nonexistent")
        assert resp.status_code == 404


class TestSearchSessions:
    async def test_empty_query(self, app_client):
        resp = await app_client.get("/api/sessions/search", params={"q": ""})
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_by_title(self, app_client):
        await app_client.post("/api/sessions", json={"title": "Python tutorial"})
        await app_client.post("/api/sessions", json={"title": "Rust guide"})
        resp = await app_client.get("/api/sessions/search", params={"q": "Python"})
        assert resp.status_code == 200
        results = resp.json()
        assert len(results) >= 1


async def test_recovered_file_paths_require_real_workspace_containment(tmp_path):
    workspace = tmp_path / "workspace"
    sibling = tmp_path / "workspace-evil"
    workspace.mkdir()
    sibling.mkdir()
    legitimate = workspace / "report.txt"
    impersonator = sibling / "stolen.txt"
    legitimate.write_text("ok", encoding="utf-8")
    impersonator.write_text("must not leak", encoding="utf-8")
    output = (
        f"Created {legitimate}\n"
        f"Created {impersonator}\n"
        f"created in {sibling}\n"
        "- stolen.txt"
    )
    message = SimpleNamespace(
        parts=[
            SimpleNamespace(
                data={
                    "type": "tool",
                    "tool": "code_execute",
                    "state": {"output": output},
                }
            )
        ]
    )

    recovered = _extract_file_paths_from_messages([message], str(workspace))

    assert recovered == [str(legitimate.resolve())]
