"""Risk disclosure shown during /connect.

DRAFT FOR APPROVAL. The wording must be honest about what the delegate
can and cannot do, and must not promise refunds or fund recovery (Ostium
is non-custodial). Luke reviews before this ships.
"""


CONNECT_DISCLOSURE = (
    "<b>Before you connect</b>\n\n"
    "1-Tap Trade lets Potion Scanner submit trades on Ostium on your "
    "behalf, using a delegate key you generate in your own browser. "
    "Here is what you should know:\n\n"
    "<b>What we can do</b>\n"
    "  - Open positions on Ostium up to the USDC allowance you grant "
    "during the one-time setup.\n"
    "  - Attach take-profit and stop-loss orders matching the original "
    "signal.\n"
    "  - That is it. Every trade is one you confirmed by tapping in "
    "Telegram.\n\n"
    "<b>What we cannot do</b>\n"
    "  - We cannot move USDC out of your wallet.\n"
    "  - We cannot place trades you did not confirm in the bot.\n"
    "  - We cannot recover funds if a position is liquidated.\n\n"
    "<b>You stay in control</b>\n"
    "  - You can revoke the delegate at any time on Ostium.\n"
    "  - You can set your USDC allowance to 0 to stop us cold.\n"
    "  - You can run /disconnect here to wipe your key from our server.\n\n"
    "<b>This is not financial advice.</b> Trading perpetual futures "
    "carries the risk of total loss. Use sizes you can afford to lose.\n\n"
    "Tap <b>I understand, continue</b> to start the one-time setup."
)


CONNECT_HOWTO = (
    "<b>One-time setup</b> (about 2 minutes)\n\n"
    "1. Open <a href=\"https://app.ostium.com/sdk-export\">"
    "app.ostium.com/sdk-export</a> in a new tab.\n"
    "2. Connect the wallet that holds the USDC you want to trade with.\n"
    "3. Tap <b>Generate Delegate Key</b>. Your browser will create a "
    "fresh key locally. <b>Copy it immediately</b>: it cannot be "
    "recovered later.\n"
    "4. Approve USDC once (this is the spending allowance we can never "
    "exceed).\n"
    "5. Register the delegate on-chain (one transaction).\n\n"
    "When that is done, paste the values back here in this exact format "
    "(one message). Tap the block to copy it, then replace both values:\n\n"
    "<pre>trader: 0xYourWalletAddress\n"
    "delegate: 0xDelegatePrivateKeyHere</pre>\n\n"
    "I will encrypt the key on our server and never log it. Use "
    "/disconnect anytime to wipe it."
)


DISCONNECT_CONFIRMED = (
    "<b>Disconnected.</b>\n\n"
    "Your delegate key has been wiped from our server. 1-Tap Trade is "
    "off for your account until you run /connect again.\n\n"
    "Heads up: this only removes the key on our side. To fully revoke "
    "the delegate on-chain, go to "
    "<a href=\"https://app.ostium.com/sdk-export\">app.ostium.com/sdk-export</a> "
    "and remove it there too, or set your USDC allowance to 0."
)
