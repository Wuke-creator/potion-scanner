"""Definitive Whop webhook signature scheme prober.

Purpose
-------
The Whop ws_ signature fix (webhook.py) assumes the secret after the
``ws_`` prefix is HEX-encoded (``bytes.fromhex``). Whop's public docs
and the Standard Webhooks spec instead say "base64-decoded secret".
The 15 unit tests pass only because they self-sign with the same hex
assumption (a false-green: it proves internal consistency, not that
Whop actually hex-encodes).

The ONLY way to settle hex-vs-base64 is one REAL captured Whop webhook.
Feed this script that single real delivery and it computes the v1
signature under every plausible key derivation, then reports which one
reproduces the signature Whop actually sent. Whichever matches is the
correct scheme; the others are wrong.

How to get the inputs (from Luke / Nourek)
------------------------------------------
From the Whop developer dashboard -> Webhooks -> the endpoint ->
Recent deliveries -> open any delivery and copy, EXACTLY:
  * the raw request body (the JSON, byte-for-byte, no reformatting)
  * header ``webhook-id``
  * header ``webhook-timestamp``
  * header ``webhook-signature``  (looks like ``v1,XXXX=`` )
Plus the signing secret already on Railway:
  ws_cc0e1db3230fa39cbf44ab6d3c8e4f45acd96601c1a471991293cd28500933b9

Usage
-----
  python tools/verify_whop_signature.py \
      --secret ws_cc0e1db3...933b9 \
      --id msg_xxxxx \
      --timestamp 1747... \
      --signature 'v1,AbC123...=' \
      --body-file /path/to/raw_body.json

(or pass --body '<raw json>' instead of --body-file)

No network, no deploy, does not import or modify webhook.py.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import hmac
import sys


def _candidates(secret: str) -> list[tuple[str, bytes]]:
    """Every plausible (label, key_bytes) derivation from the secret."""
    out: list[tuple[str, bytes]] = []

    after_ws = secret[3:] if secret.startswith("ws_") else secret
    after_whsec = secret[len("whsec_"):] if secret.startswith("whsec_") else secret

    # A. ws_ stripped, HEX decoded  -> what the concurrent fix does
    try:
        out.append(("ws_ stripped + HEX decode  [concurrent fix]",
                     bytes.fromhex(after_ws)))
    except ValueError:
        pass
    # B. ws_ stripped, BASE64 decoded -> Whop docs / Standard Webhooks
    try:
        out.append(("ws_ stripped + BASE64 decode  [Whop docs]",
                     base64.b64decode(after_ws)))
    except (ValueError, binascii.Error):
        pass
    # C. ws_ stripped, raw UTF-8 bytes
    out.append(("ws_ stripped + raw utf-8", after_ws.encode()))
    # D. full secret (incl ws_) raw UTF-8
    out.append(("full secret raw utf-8", secret.encode()))
    # E. full secret BASE64 decoded, validate=False  [original buggy path]
    try:
        out.append(("full secret BASE64 (validate=False)  [original bug]",
                     base64.b64decode(secret, validate=False)))
    except (ValueError, binascii.Error):
        pass
    # F. whsec_ stripped + BASE64 (in case secret is pasted that way)
    if secret.startswith("whsec_"):
        try:
            out.append(("whsec_ stripped + BASE64 decode",
                         base64.b64decode(after_whsec)))
        except (ValueError, binascii.Error):
            pass
    return out


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--secret", required=True)
    p.add_argument("--id", required=True, help="webhook-id header")
    p.add_argument("--timestamp", required=True, help="webhook-timestamp header")
    p.add_argument("--signature", required=True,
                   help="webhook-signature header, e.g. 'v1,AbC...='")
    p.add_argument("--body")
    p.add_argument("--body-file")
    a = p.parse_args()

    if a.body_file:
        with open(a.body_file, "rb") as fh:
            body = fh.read()
    elif a.body is not None:
        body = a.body.encode("utf-8")
    else:
        print("ERROR: pass --body or --body-file", file=sys.stderr)
        return 2

    # Standard Webhooks signed content: "{id}.{timestamp}.{body}"
    signed = f"{a.id}.{a.timestamp}.".encode("utf-8") + body

    # Pull every v1 token from the received header (space separated for
    # key rotation; any single match is a pass).
    received = {
        e.split(",", 1)[1].strip()
        for e in a.signature.split()
        if e.startswith("v1,") and "," in e
    }
    if not received:
        # tolerate a bare base64 with no v1, prefix
        received = {a.signature.strip()}

    print(f"signed content length: {len(signed)} bytes")
    print(f"received v1 signature(s): {sorted(received)}")
    print("-" * 72)

    match = None
    for label, key in _candidates(a.secret):
        digest = hmac.new(key, signed, hashlib.sha256).digest()
        b64 = base64.b64encode(digest).decode("ascii")
        hexd = digest.hex()
        ok = b64 in received or hexd in received
        flag = "  <==== MATCH" if ok else ""
        print(f"[{'PASS' if ok else 'fail'}] {label}")
        print(f"        key={len(key)}B  b64={b64}{flag}")
        if ok:
            match = label

    print("-" * 72)
    if match:
        print(f"RESULT: Whop uses -> {match}")
        print("Action: webhook.py's ws_ branch must use THIS derivation.")
        return 0
    print("RESULT: NO derivation matched.")
    print("Either the body was altered (reformatted/whitespace), the "
          "secret is stale/rotated, or the headers/body are mismatched. "
          "Recapture the raw delivery byte-for-byte and retry.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
