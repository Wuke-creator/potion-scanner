"""Discord OAuth 2.0 + PKCE client.

Four operations:

  1. ``new_pkce_pair()`` — generates a verifier + S256 challenge pair.
  2. ``build_authorize_url(state, code_challenge, redirect_uri)`` — assembles
     the URL we send to the Telegram user as part of /verify.
  3. ``exchange_code(code, code_verifier, redirect_uri)`` — turns an
     authorization code into ``(access_token, refresh_token)``.
  4. ``refresh_access_token(refresh_token)`` — used by the reverify cron.
  5. ``check_elite_role(access_token, guild_id)`` — calls
     ``/users/@me/guilds/{guild_id}/member`` and returns an ``EliteMember``
     struct if the user currently holds the configured Elite role.

Unlike Whop, Discord has no "membership" abstraction — access is keyed off
a **role** in a specific guild. The membership check is therefore
``elite_role_id in member.roles``.

Important notes about the Discord API:

- ``/users/@me/guilds/{guild_id}/member`` requires the ``guilds.members.read``
  scope. It returns a 404 if the user is not a member of the guild (which
  we surface as "not in Potion — join the server first").
- The token endpoint uses ``application/x-www-form-urlencoded`` and HTTP
  Basic auth is NOT required; we pass ``client_id`` and ``client_secret``
  as form fields, which Discord accepts.
- Discord returns 401 if the access token has expired. The reverify cron
  always refreshes first, so callers should get a fresh token before
  calling ``check_elite_role``.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import secrets
from dataclasses import dataclass
from urllib.parse import urlencode

import aiohttp

from src.config import DiscordOAuthConfig

logger = logging.getLogger(__name__)


class DiscordOAuthError(Exception):
    """Raised on any failure during the Discord OAuth flow."""


class DiscordNotInGuildError(DiscordOAuthError):
    """User authorized but is not a member of the configured guild."""


@dataclass
class DiscordTokens:
    access_token: str
    refresh_token: str
    expires_in: int       # seconds
    token_type: str       # "Bearer"
    scope: str


@dataclass
class EliteMember:
    """A Discord guild member confirmed to hold the Elite role."""

    discord_user_id: str
    username: str
    nick: str | None
    roles: list[str]
    email: str = ""           # populated if the `email` OAuth scope was granted


def _b64url_no_pad(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def new_pkce_pair() -> tuple[str, str]:
    """Generate (code_verifier, code_challenge) for PKCE S256."""
    verifier = _b64url_no_pad(secrets.token_bytes(48))
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = _b64url_no_pad(digest)
    return verifier, challenge


class DiscordOAuthClient:
    """Async client for the Discord OAuth2 + guild member endpoints."""

    def __init__(
        self,
        config: DiscordOAuthConfig,
        session: aiohttp.ClientSession | None = None,
    ):
        self._config = config
        self._owns_session = session is None
        self._session = session

    async def __aenter__(self) -> "DiscordOAuthClient":
        if self._session is None:
            self._session = aiohttp.ClientSession()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._owns_session and self._session is not None:
            await self._session.close()
            self._session = None

    async def close(self) -> None:
        if self._owns_session and self._session is not None:
            await self._session.close()
            self._session = None

    def build_authorize_url(
        self, state: str, code_challenge: str, redirect_uri: str,
    ) -> str:
        params = {
            "client_id": self._config.client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": self._config.scope,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "state": state,
            "prompt": "consent",
        }
        return f"{self._config.authorize_url}?{urlencode(params)}"

    async def exchange_code(
        self, code: str, code_verifier: str, redirect_uri: str,
    ) -> DiscordTokens:
        return await self._token_request({
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": code_verifier,
            "redirect_uri": redirect_uri,
            "client_id": self._config.client_id,
            "client_secret": self._config.client_secret,
        })

    async def refresh_access_token(self, refresh_token: str) -> DiscordTokens:
        return await self._token_request({
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": self._config.client_id,
            "client_secret": self._config.client_secret,
        })

    async def _token_request(self, payload: dict) -> DiscordTokens:
        assert self._session is not None
        try:
            async with self._session.post(
                self._config.token_url,
                data=payload,
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    raise DiscordOAuthError(
                        f"Discord token endpoint returned {resp.status}: {body[:200]}"
                    )
                data = await resp.json()
        except aiohttp.ClientError as e:
            raise DiscordOAuthError(f"Discord token request failed: {e}") from e

        access = data.get("access_token")
        refresh = data.get("refresh_token")
        if not access or not refresh:
            raise DiscordOAuthError(
                "Discord token response missing access_token or refresh_token"
            )
        return DiscordTokens(
            access_token=access,
            refresh_token=refresh,
            expires_in=int(data.get("expires_in", 0)),
            token_type=data.get("token_type", "Bearer"),
            scope=data.get("scope", ""),
        )

    async def fetch_email(self, access_token: str) -> str:
        """Return the user's email from Discord /users/@me.

        Only populated if the `email` OAuth scope was granted during
        authorization. Returns empty string on any failure so callers can
        degrade gracefully (we never block verification just because email
        wasn't available).
        """
        assert self._session is not None
        url = f"{self._config.api_base.rstrip('/')}/users/@me"
        try:
            async with self._session.get(
                url,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Accept": "application/json",
                },
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    return ""
                data = await resp.json()
        except aiohttp.ClientError:
            return ""
        email = data.get("email") if isinstance(data, dict) else None
        verified = data.get("verified") if isinstance(data, dict) else None
        # Only trust verified Discord emails so we don't email bounces
        if email and verified:
            return str(email).strip().lower()
        return ""

    async def check_elite_role(
        self, access_token: str, guild_id: int,
    ) -> EliteMember | None:
        """Call GET /users/@me/guilds/{guild_id}/member and check for the Elite role.

        Returns an ``EliteMember`` if the user holds the role, ``None`` if
        they're in the guild but missing the role, and raises
        ``DiscordNotInGuildError`` if they're not in the guild at all.
        """
        assert self._session is not None
        url = f"{self._config.api_base.rstrip('/')}/users/@me/guilds/{guild_id}/member"
        try:
            async with self._session.get(
                url,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Accept": "application/json",
                },
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status == 401:
                    raise DiscordOAuthError("Discord access token rejected (401)")
                if resp.status == 404:
                    raise DiscordNotInGuildError(
                        "user is not a member of the Potion guild"
                    )
                if resp.status != 200:
                    body = await resp.text()
                    raise DiscordOAuthError(
                        f"Discord member endpoint returned {resp.status}: {body[:200]}"
                    )
                data = await resp.json()
        except aiohttp.ClientError as e:
            raise DiscordOAuthError(f"Discord member request failed: {e}") from e

        roles = [str(r) for r in data.get("roles", [])]
        if self._config.elite_role_id not in roles:
            return None

        user_obj = data.get("user") or {}
        return EliteMember(
            discord_user_id=str(user_obj.get("id", "")),
            username=str(user_obj.get("username", "")),
            nick=data.get("nick"),
            roles=roles,
        )
