"""Tests for the Admin REST API."""

import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
import pytest_asyncio
from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase, TestClient, TestServer
from cryptography.fernet import Fernet

from src.api.admin import AdminAPI, auth_middleware
from src.crypto import reset_fernet
from src.state.user_db import UserDatabase

API_KEY = "test-api-key-12345"

SAMPLE_CREDS = {
    "account_address": "0xABC123",
    "api_wallet": "0xWALLET456",
    "api_secret": "0xSECRET789",
}


@pytest.fixture(autouse=True)
def _encryption_key():
    key = Fernet.generate_key()
    reset_fernet()
    with patch.dict(os.environ, {"ENCRYPTION_KEY": key.decode(), "ADMIN_API_KEY": API_KEY}):
        yield
    reset_fernet()


@pytest.fixture
def user_db():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        udb = UserDatabase(db_path=db_path)
        yield udb
        udb.close()


@pytest_asyncio.fixture
async def api_client(user_db):
    """Create a test client for the admin API."""
    activated = []
    deactivated = []
    kill_results = {}

    async def on_activate(user_id):
        activated.append(user_id)

    async def on_deactivate(user_id):
        deactivated.append(user_id)

    async def on_kill():
        return kill_results

    async def on_resume():
        pass

    admin = AdminAPI(
        user_db=user_db,
        on_user_activate=on_activate,
        on_user_deactivate=on_deactivate,
        on_kill=on_kill,
        on_resume=on_resume,
    )
    server = TestServer(admin._app)
    client = TestClient(server)
    await client.start_server()
    client._activated = activated
    client._deactivated = deactivated
    yield client
    await client.close()


def _headers():
    return {"X-API-Key": API_KEY}


class TestAuthMiddleware:
    @pytest.mark.asyncio
    async def test_rejects_missing_key(self, api_client):
        resp = await api_client.get("/api/users")
        assert resp.status == 401

    @pytest.mark.asyncio
    async def test_rejects_wrong_key(self, api_client):
        resp = await api_client.get("/api/users", headers={"X-API-Key": "wrong"})
        assert resp.status == 401

    @pytest.mark.asyncio
    async def test_accepts_correct_key(self, api_client):
        resp = await api_client.get("/api/users", headers=_headers())
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_rejects_when_no_api_key_configured(self, api_client):
        with patch.dict(os.environ, {"ADMIN_API_KEY": ""}):
            resp = await api_client.get("/api/users", headers=_headers())
            assert resp.status == 503


class TestCreateUser:
    @pytest.mark.asyncio
    async def test_create_user(self, api_client):
        resp = await api_client.post(
            "/api/users",
            json={
                "user_id": "alice",
                "display_name": "Alice",
                "credentials": SAMPLE_CREDS,
            },
            headers=_headers(),
        )
        assert resp.status == 201
        data = await resp.json()
        assert data["user_id"] == "alice"
        assert data["status"] == "active"

    @pytest.mark.asyncio
    async def test_create_user_triggers_activate_callback(self, api_client):
        await api_client.post(
            "/api/users",
            json={"user_id": "alice", "display_name": "Alice", "credentials": SAMPLE_CREDS},
            headers=_headers(),
        )
        assert "alice" in api_client._activated

    @pytest.mark.asyncio
    async def test_create_user_with_config(self, api_client):
        resp = await api_client.post(
            "/api/users",
            json={
                "user_id": "bob",
                "display_name": "Bob",
                "credentials": SAMPLE_CREDS,
                "config": {"active_preset": "conservative", "max_leverage": 10},
            },
            headers=_headers(),
        )
        assert resp.status == 201

    @pytest.mark.asyncio
    async def test_create_user_missing_fields(self, api_client):
        resp = await api_client.post(
            "/api/users",
            json={"user_id": "alice"},
            headers=_headers(),
        )
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_create_user_missing_credential_fields(self, api_client):
        resp = await api_client.post(
            "/api/users",
            json={
                "user_id": "alice",
                "display_name": "Alice",
                "credentials": {"account_address": "0x1"},
            },
            headers=_headers(),
        )
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_create_duplicate_user(self, api_client):
        await api_client.post(
            "/api/users",
            json={"user_id": "alice", "display_name": "Alice", "credentials": SAMPLE_CREDS},
            headers=_headers(),
        )
        resp = await api_client.post(
            "/api/users",
            json={"user_id": "alice", "display_name": "Alice2", "credentials": SAMPLE_CREDS},
            headers=_headers(),
        )
        assert resp.status == 409


class TestListUsers:
    @pytest.mark.asyncio
    async def test_list_empty(self, api_client):
        resp = await api_client.get("/api/users", headers=_headers())
        assert resp.status == 200
        data = await resp.json()
        assert data == []

    @pytest.mark.asyncio
    async def test_list_multiple(self, api_client):
        for uid in ("alice", "bob"):
            await api_client.post(
                "/api/users",
                json={"user_id": uid, "display_name": uid.title(), "credentials": SAMPLE_CREDS},
                headers=_headers(),
            )
        resp = await api_client.get("/api/users", headers=_headers())
        data = await resp.json()
        assert len(data) == 2

    @pytest.mark.asyncio
    async def test_list_filter_by_status(self, api_client, user_db):
        for uid in ("alice", "bob"):
            await api_client.post(
                "/api/users",
                json={"user_id": uid, "display_name": uid.title(), "credentials": SAMPLE_CREDS},
                headers=_headers(),
            )
        user_db.set_user_status("bob", "inactive")

        resp = await api_client.get("/api/users?status=active", headers=_headers())
        data = await resp.json()
        assert len(data) == 1
        assert data[0]["user_id"] == "alice"


class TestGetUser:
    @pytest.mark.asyncio
    async def test_get_user(self, api_client):
        await api_client.post(
            "/api/users",
            json={"user_id": "alice", "display_name": "Alice", "credentials": SAMPLE_CREDS},
            headers=_headers(),
        )
        resp = await api_client.get("/api/users/alice", headers=_headers())
        assert resp.status == 200
        data = await resp.json()
        assert data["user_id"] == "alice"
        assert "config" in data
        # No secrets should be exposed
        assert "api_secret" not in str(data)

    @pytest.mark.asyncio
    async def test_get_nonexistent_user(self, api_client):
        resp = await api_client.get("/api/users/nobody", headers=_headers())
        assert resp.status == 404


class TestUpdateUser:
    @pytest.mark.asyncio
    async def test_update_config(self, api_client, user_db):
        await api_client.post(
            "/api/users",
            json={"user_id": "alice", "display_name": "Alice", "credentials": SAMPLE_CREDS},
            headers=_headers(),
        )
        resp = await api_client.put(
            "/api/users/alice",
            json={"config": {"max_leverage": 15, "active_preset": "tp2_exit"}},
            headers=_headers(),
        )
        assert resp.status == 200
        cfg = user_db.get_user_config("alice")
        assert cfg["max_leverage"] == 15
        assert cfg["active_preset"] == "tp2_exit"

    @pytest.mark.asyncio
    async def test_update_nonexistent_user(self, api_client):
        resp = await api_client.put(
            "/api/users/nobody",
            json={"config": {"max_leverage": 15}},
            headers=_headers(),
        )
        assert resp.status == 404


class TestActivateDeactivate:
    @pytest.mark.asyncio
    async def test_deactivate_user(self, api_client, user_db):
        await api_client.post(
            "/api/users",
            json={"user_id": "alice", "display_name": "Alice", "credentials": SAMPLE_CREDS},
            headers=_headers(),
        )
        resp = await api_client.post("/api/users/alice/deactivate", headers=_headers())
        assert resp.status == 200
        data = await resp.json()
        assert data["status"] == "inactive"
        assert user_db.get_user("alice").status == "inactive"
        assert "alice" in api_client._deactivated

    @pytest.mark.asyncio
    async def test_activate_user(self, api_client, user_db):
        await api_client.post(
            "/api/users",
            json={"user_id": "alice", "display_name": "Alice", "credentials": SAMPLE_CREDS},
            headers=_headers(),
        )
        user_db.set_user_status("alice", "inactive")
        api_client._activated.clear()

        resp = await api_client.post("/api/users/alice/activate", headers=_headers())
        assert resp.status == 200
        data = await resp.json()
        assert data["status"] == "active"
        assert "alice" in api_client._activated

    @pytest.mark.asyncio
    async def test_delete_deactivates(self, api_client, user_db):
        await api_client.post(
            "/api/users",
            json={"user_id": "alice", "display_name": "Alice", "credentials": SAMPLE_CREDS},
            headers=_headers(),
        )
        resp = await api_client.delete("/api/users/alice", headers=_headers())
        assert resp.status == 200
        assert user_db.get_user("alice").status == "inactive"

    @pytest.mark.asyncio
    async def test_deactivate_nonexistent(self, api_client):
        resp = await api_client.post("/api/users/nobody/deactivate", headers=_headers())
        assert resp.status == 404


class TestKillSwitch:
    @pytest.mark.asyncio
    async def test_kill_endpoint(self, api_client):
        resp = await api_client.post("/api/kill", headers=_headers())
        assert resp.status == 200
        data = await resp.json()
        assert data["status"] == "killed"

    @pytest.mark.asyncio
    async def test_resume_endpoint(self, api_client):
        resp = await api_client.post("/api/resume", headers=_headers())
        assert resp.status == 200
        data = await resp.json()
        assert data["status"] == "resumed"

    @pytest.mark.asyncio
    async def test_kill_requires_auth(self, api_client):
        resp = await api_client.post("/api/kill")
        assert resp.status == 401

    @pytest.mark.asyncio
    async def test_resume_requires_auth(self, api_client):
        resp = await api_client.post("/api/resume")
        assert resp.status == 401
