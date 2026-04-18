"""Whop OAuth verification subsystem.

Gates the Telegram Elite group to verified Whop members. Composed of:

  - state_token.py: HMAC-signed CSRF state for the OAuth round-trip
  - db.py:          aiosqlite store for verified_users + pending_verifications
  - whop_oauth.py:  PKCE, token exchange, /me/memberships check
  - oauth_callback.py: aiohttp web server hosting /oauth/whop/callback
  - commands.py:    Telegram /start /verify /status /help command handlers
  - reverify_job.py: 24h cron — re-checks every active user, kicks lapsed
  - runtime.py:     glue that wires everything into a single lifecycle handle
"""
