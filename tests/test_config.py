"""Tests for src/config/settings.py — env loading and channel routing."""

import os
from pathlib import Path

import pytest

from src.config.settings import (
    SOURCE_MEMECOIN,
    SOURCE_PERPS,
    ConfigError,
    _build_channel_routes,
    load_config,
)


# All env vars the validator requires; tests override what they need
ALL_REQUIRED = {
    "DISCORD_BOT_TOKEN": "fake-discord-bot-token",
    "POTION_GUILD_ID": "1260259552763580537",
    "PERP_BOT_CHANNEL_ID": "1445440392509132850",
    "MANUAL_PERP_CHANNEL_ID": "1316518499283370064",
    "PREDICTION_CHANNEL_ID": "1420272690459181118",
    "TELEGRAM_BOT_TOKEN": "fake-telegram-token",
    "REF_LINK_PERPS": "https://partner.blofin.com/d/potion",
    "REF_LINK_MEMECOIN": "https://trade.padre.gg/rk/orangie",
    "DISCORD_OAUTH_CLIENT_ID": "1492697810120740975",
    "DISCORD_OAUTH_CLIENT_SECRET": "fake-oauth-client-secret",
    "DISCORD_ELITE_ROLE_ID": "1111111111111111111",
    "ELITE_SIGNUP_URL": "https://whop.com/potion",
    "OAUTH_REDIRECT_URI": "https://example.com/oauth/discord/callback",
    "OAUTH_STATE_SECRET": "0123456789abcdef0123456789abcdef",
    "WHOP_REFRESH_TOKEN_ENCRYPTION_KEY": "fake-fernet-key",
}


@pytest.fixture
def env_with_required(monkeypatch):
    for k, v in ALL_REQUIRED.items():
        monkeypatch.setenv(k, v)
    yield


def _write_config_yaml(path: Path) -> None:
    path.write_text(
        """
discord:
  channels:
    - key: perp_bot
      name: "Perp Bot Calls"
      source_type: perps
      id_env: PERP_BOT_CHANNEL_ID
      ref_link_env: REF_LINK_PERPS

    - key: manual_perp
      name: "Manual Perp Calls"
      source_type: perps
      id_env: MANUAL_PERP_CHANNEL_ID
      ref_link_env: REF_LINK_PERPS

    - key: prediction
      name: "Prediction Calls"
      source_type: memecoin
      id_env: PREDICTION_CHANNEL_ID
      ref_link_env: REF_LINK_MEMECOIN
"""
    )


class TestBuildChannelRoutes:
    def test_resolves_three_channels_from_env(self, env_with_required):
        routes = _build_channel_routes([
            {
                "key": "perp_bot",
                "name": "Perp Bot Calls",
                "source_type": "perps",
                "id_env": "PERP_BOT_CHANNEL_ID",
                "ref_link_env": "REF_LINK_PERPS",
            },
            {
                "key": "manual_perp",
                "name": "Manual Perp Calls",
                "source_type": "perps",
                "id_env": "MANUAL_PERP_CHANNEL_ID",
                "ref_link_env": "REF_LINK_PERPS",
            },
            {
                "key": "prediction",
                "name": "Prediction Calls",
                "source_type": "memecoin",
                "id_env": "PREDICTION_CHANNEL_ID",
                "ref_link_env": "REF_LINK_MEMECOIN",
            },
        ])
        assert len(routes) == 3
        assert routes[0].key == "perp_bot"
        assert routes[0].channel_id == 1445440392509132850
        assert routes[0].source_type == SOURCE_PERPS
        assert routes[0].ref_link == "https://partner.blofin.com/d/potion"
        assert routes[2].key == "prediction"
        assert routes[2].channel_id == 1420272690459181118
        assert routes[2].source_type == SOURCE_MEMECOIN
        assert routes[2].ref_link == "https://trade.padre.gg/rk/orangie"

    def test_duplicate_key_raises(self, env_with_required):
        with pytest.raises(ConfigError, match="duplicate"):
            _build_channel_routes([
                {
                    "key": "perp_bot",
                    "name": "A",
                    "source_type": "perps",
                    "id_env": "PERP_BOT_CHANNEL_ID",
                    "ref_link_env": "REF_LINK_PERPS",
                },
                {
                    "key": "perp_bot",
                    "name": "B",
                    "source_type": "perps",
                    "id_env": "MANUAL_PERP_CHANNEL_ID",
                    "ref_link_env": "REF_LINK_PERPS",
                },
            ])

    def test_invalid_source_type_raises(self, env_with_required):
        with pytest.raises(ConfigError, match="source_type"):
            _build_channel_routes([
                {
                    "key": "bad",
                    "name": "Bad",
                    "source_type": "futures",
                    "id_env": "PERP_BOT_CHANNEL_ID",
                    "ref_link_env": "REF_LINK_PERPS",
                },
            ])

    def test_missing_id_env_raises(self):
        with pytest.raises(ConfigError, match="id_env"):
            _build_channel_routes([
                {
                    "key": "bad",
                    "name": "Bad",
                    "source_type": "perps",
                    "ref_link_env": "REF_LINK_PERPS",
                },
            ])

    def test_missing_ref_link_in_env_raises(self, monkeypatch):
        monkeypatch.setenv("PERP_BOT_CHANNEL_ID", "1234")
        monkeypatch.delenv("REF_LINK_PERPS", raising=False)
        with pytest.raises(ConfigError, match="REF_LINK_PERPS"):
            _build_channel_routes([
                {
                    "key": "perp_bot",
                    "name": "Perp Bot Calls",
                    "source_type": "perps",
                    "id_env": "PERP_BOT_CHANNEL_ID",
                    "ref_link_env": "REF_LINK_PERPS",
                },
            ])


class TestLoadConfig:
    def test_loads_full_config(self, env_with_required, tmp_path):
        cfg_path = tmp_path / "config.yaml"
        _write_config_yaml(cfg_path)

        config = load_config(config_path=cfg_path, env_file=tmp_path / ".env")

        assert config.discord.bot_token == "fake-discord-bot-token"
        assert config.discord.guild_id == 1260259552763580537
        assert len(config.discord.channels) == 3
        assert config.telegram.bot_token == "fake-telegram-token"
        assert config.discord_oauth.client_id == "1492697810120740975"
        assert config.discord_oauth.elite_role_id == "1111111111111111111"
        assert config.discord_oauth.elite_signup_url == "https://whop.com/potion"
        assert config.oauth.redirect_uri == "https://example.com/oauth/discord/callback"
        assert config.dispatcher.rate_per_sec > 0
        assert config.dispatcher.max_concurrent > 0

    def test_missing_required_secret_raises(self, env_with_required, tmp_path, monkeypatch):
        monkeypatch.delenv("DISCORD_OAUTH_CLIENT_SECRET")
        cfg_path = tmp_path / "config.yaml"
        _write_config_yaml(cfg_path)
        with pytest.raises(ConfigError, match="DISCORD_OAUTH_CLIENT_SECRET"):
            load_config(config_path=cfg_path, env_file=tmp_path / ".env")

    def test_missing_elite_role_id_raises(self, env_with_required, tmp_path, monkeypatch):
        monkeypatch.delenv("DISCORD_ELITE_ROLE_ID")
        cfg_path = tmp_path / "config.yaml"
        _write_config_yaml(cfg_path)
        with pytest.raises(ConfigError, match="DISCORD_ELITE_ROLE_ID"):
            load_config(config_path=cfg_path, env_file=tmp_path / ".env")
