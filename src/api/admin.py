"""Admin REST API for user management.

Runs an aiohttp server alongside the bot in the same event loop.
Protected by ADMIN_API_KEY via middleware.
"""

import logging
import os
from typing import Any, Awaitable, Callable

from aiohttp import web

from src.state.user_db import UserDatabase

logger = logging.getLogger(__name__)

OnUserCallback = Callable[[str], Awaitable[None]]
OnKillCallback = Callable[[], Awaitable[dict]]
OnResumeCallback = Callable[[], Awaitable[None]]


@web.middleware
async def auth_middleware(
    request: web.Request, handler: Callable[..., Awaitable[web.StreamResponse]]
) -> web.StreamResponse:
    """Check X-API-Key header against ADMIN_API_KEY env var."""
    api_key = os.getenv("ADMIN_API_KEY", "")
    if not api_key:
        raise web.HTTPServiceUnavailable(text="ADMIN_API_KEY not configured")

    provided = request.headers.get("X-API-Key", "")
    if provided != api_key:
        raise web.HTTPUnauthorized(text="Invalid or missing API key")

    return await handler(request)


class AdminAPI:
    """aiohttp-based admin REST API for managing users.

    Args:
        user_db: UserDatabase instance for CRUD operations.
        on_user_activate: Async callback when a user is activated.
        on_user_deactivate: Async callback when a user is deactivated.
        port: Port to listen on (default from ADMIN_API_PORT env var or 8081).
    """

    def __init__(
        self,
        user_db: UserDatabase,
        on_user_activate: OnUserCallback | None = None,
        on_user_deactivate: OnUserCallback | None = None,
        on_kill: OnKillCallback | None = None,
        on_resume: OnResumeCallback | None = None,
        port: int | None = None,
    ):
        self._user_db = user_db
        self._on_user_activate = on_user_activate
        self._on_user_deactivate = on_user_deactivate
        self._on_kill = on_kill
        self._on_resume = on_resume
        self._port = port or int(os.getenv("ADMIN_API_PORT", "8081"))
        self._runner: web.AppRunner | None = None
        self._app = self._build_app()

    def _build_app(self) -> web.Application:
        app = web.Application(middlewares=[auth_middleware])
        app.router.add_post("/api/users", self._create_user)
        app.router.add_get("/api/users", self._list_users)
        app.router.add_get("/api/users/{user_id}", self._get_user)
        app.router.add_put("/api/users/{user_id}", self._update_user)
        app.router.add_delete("/api/users/{user_id}", self._deactivate_user)
        app.router.add_post("/api/users/{user_id}/activate", self._activate_user)
        app.router.add_post("/api/users/{user_id}/deactivate", self._deactivate_user)
        app.router.add_post("/api/kill", self._kill)
        app.router.add_post("/api/resume", self._resume)
        return app

    async def start(self) -> None:
        """Start the admin API server."""
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "0.0.0.0", self._port)
        await site.start()
        logger.info("Admin API listening on port %d", self._port)

    async def stop(self) -> None:
        """Stop the admin API server."""
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
            logger.info("Admin API stopped")

    # ------------------------------------------------------------------
    # Handlers
    # ------------------------------------------------------------------

    async def _create_user(self, request: web.Request) -> web.Response:
        """POST /api/users — Create a new user."""
        try:
            body = await request.json()
        except Exception:
            raise web.HTTPBadRequest(text="Invalid JSON body")

        user_id = body.get("user_id")
        display_name = body.get("display_name")
        credentials = body.get("credentials")

        if not user_id or not display_name or not credentials:
            raise web.HTTPBadRequest(text="user_id, display_name, and credentials are required")

        for field in ("account_address", "api_wallet", "api_secret"):
            if field not in credentials:
                raise web.HTTPBadRequest(text=f"credentials.{field} is required")

        config = body.get("config", {})

        try:
            user = self._user_db.create_user(user_id, display_name, credentials, config)
        except Exception as e:
            logger.error("Failed to create user %s: %s", user_id, e)
            raise web.HTTPConflict(text=f"Failed to create user: {e}")

        # Trigger activation callback for newly created active users
        if self._on_user_activate:
            try:
                await self._on_user_activate(user_id)
            except Exception as e:
                logger.error("Activation callback failed for %s: %s", user_id, e)

        return web.json_response(
            {"user_id": user.user_id, "display_name": user.display_name, "status": user.status},
            status=201,
        )

    async def _list_users(self, request: web.Request) -> web.Response:
        """GET /api/users — List users, optionally filtered by status."""
        status = request.query.get("status")
        users = self._user_db.list_users(status=status)
        return web.json_response([
            {"user_id": u.user_id, "display_name": u.display_name, "status": u.status}
            for u in users
        ])

    async def _get_user(self, request: web.Request) -> web.Response:
        """GET /api/users/{user_id} — Get user detail (config + status, no secrets)."""
        user_id = request.match_info["user_id"]
        user = self._user_db.get_user(user_id)
        if not user:
            raise web.HTTPNotFound(text=f"User {user_id} not found")

        config = self._user_db.get_user_config(user_id)
        return web.json_response({
            "user_id": user.user_id,
            "display_name": user.display_name,
            "status": user.status,
            "config": config,
            "created_at": user.created_at,
            "updated_at": user.updated_at,
        })

    async def _update_user(self, request: web.Request) -> web.Response:
        """PUT /api/users/{user_id} — Update user config."""
        user_id = request.match_info["user_id"]
        user = self._user_db.get_user(user_id)
        if not user:
            raise web.HTTPNotFound(text=f"User {user_id} not found")

        try:
            body = await request.json()
        except Exception:
            raise web.HTTPBadRequest(text="Invalid JSON body")

        # Update display_name if provided
        if "display_name" in body:
            now = __import__("datetime").datetime.utcnow().isoformat()
            with self._user_db._conn:
                self._user_db._conn.execute(
                    "UPDATE users SET display_name = ?, updated_at = ? WHERE user_id = ?",
                    (body["display_name"], now, user_id),
                )

        # Update config fields if provided
        config_fields = body.get("config", {})
        if config_fields:
            self._user_db.update_user_config(user_id, **config_fields)

        # Update credentials if provided
        cred_fields = body.get("credentials", {})
        if cred_fields:
            self._user_db.update_user_credentials(user_id, **cred_fields)

        return web.json_response({"status": "updated", "user_id": user_id})

    async def _activate_user(self, request: web.Request) -> web.Response:
        """POST /api/users/{user_id}/activate — Activate user."""
        user_id = request.match_info["user_id"]
        user = self._user_db.get_user(user_id)
        if not user:
            raise web.HTTPNotFound(text=f"User {user_id} not found")

        self._user_db.set_user_status(user_id, "active")

        if self._on_user_activate:
            try:
                await self._on_user_activate(user_id)
            except Exception as e:
                logger.error("Activation callback failed for %s: %s", user_id, e)

        return web.json_response({"status": "active", "user_id": user_id})

    async def _deactivate_user(self, request: web.Request) -> web.Response:
        """DELETE /api/users/{user_id} or POST /api/users/{user_id}/deactivate."""
        user_id = request.match_info["user_id"]
        user = self._user_db.get_user(user_id)
        if not user:
            raise web.HTTPNotFound(text=f"User {user_id} not found")

        self._user_db.set_user_status(user_id, "inactive")

        if self._on_user_deactivate:
            try:
                await self._on_user_deactivate(user_id)
            except Exception as e:
                logger.error("Deactivation callback failed for %s: %s", user_id, e)

        return web.json_response({"status": "inactive", "user_id": user_id})

    async def _kill(self, request: web.Request) -> web.Response:
        """POST /api/kill — Emergency kill switch: cancel orders and close all positions."""
        if not self._on_kill:
            raise web.HTTPServiceUnavailable(text="Kill switch not configured")

        try:
            results = await self._on_kill()
        except Exception as e:
            logger.error("Kill switch error: %s", e)
            raise web.HTTPInternalServerError(text=f"Kill switch error: {e}")

        return web.json_response({"status": "killed", "results": results})

    async def _resume(self, request: web.Request) -> web.Response:
        """POST /api/resume — Resume signal processing after kill switch."""
        if not self._on_resume:
            raise web.HTTPServiceUnavailable(text="Resume not configured")

        try:
            await self._on_resume()
        except Exception as e:
            logger.error("Resume error: %s", e)
            raise web.HTTPInternalServerError(text=f"Resume error: {e}")

        return web.json_response({"status": "resumed"})
