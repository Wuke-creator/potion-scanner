"""Tests for src/verification/discord_oauth.py — PKCE + URL building.

Network-bound methods (token exchange, member fetch) are exercised in
end-to-end staging — these tests cover the pure pieces.
"""

import hashlib
from urllib.parse import parse_qs, urlparse

from src.config import DiscordOAuthConfig
from src.verification.discord_oauth import DiscordOAuthClient, new_pkce_pair


class TestNewPkcePair:
    def test_returns_two_strings(self):
        verifier, challenge = new_pkce_pair()
        assert isinstance(verifier, str) and len(verifier) > 30
        assert isinstance(challenge, str) and len(challenge) > 30

    def test_each_pair_is_unique(self):
        a = new_pkce_pair()
        b = new_pkce_pair()
        assert a[0] != b[0]
        assert a[1] != b[1]

    def test_challenge_is_sha256_of_verifier_base64url(self):
        import base64

        verifier, challenge = new_pkce_pair()
        digest = hashlib.sha256(verifier.encode("ascii")).digest()
        expected = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
        assert challenge == expected


class TestBuildAuthorizeUrl:
    def _client(self) -> DiscordOAuthClient:
        cfg = DiscordOAuthConfig(
            client_id="1492697810120740975",
            client_secret="test_secret",
            elite_role_id="1111111111111111111",
            authorize_url="https://discord.com/api/oauth2/authorize",
            scope="identify guilds.members.read",
        )
        return DiscordOAuthClient(cfg)

    def test_url_contains_all_required_params(self):
        client = self._client()
        url = client.build_authorize_url(
            state="state-xyz",
            code_challenge="challenge-xyz",
            redirect_uri="https://example.com/oauth/discord/callback",
        )
        parsed = urlparse(url)
        assert parsed.scheme == "https"
        assert parsed.netloc == "discord.com"
        assert parsed.path == "/api/oauth2/authorize"

        params = parse_qs(parsed.query)
        assert params["client_id"] == ["1492697810120740975"]
        assert params["redirect_uri"] == ["https://example.com/oauth/discord/callback"]
        assert params["response_type"] == ["code"]
        assert params["scope"] == ["identify guilds.members.read"]
        assert params["state"] == ["state-xyz"]
        assert params["code_challenge"] == ["challenge-xyz"]
        assert params["code_challenge_method"] == ["S256"]
        assert params["prompt"] == ["consent"]
