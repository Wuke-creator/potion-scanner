"""Invite code generation and validation logic."""

import secrets
import string


def generate_invite_code() -> str:
    """Generate a unique invite code in PPB-XXXX-XXXX format.

    Uses uppercase letters and digits, excluding ambiguous characters (0/O, 1/I/L).
    """
    alphabet = string.ascii_uppercase + string.digits
    # Remove ambiguous characters
    alphabet = alphabet.replace("O", "").replace("0", "").replace("I", "").replace("L", "").replace("1", "")

    part1 = "".join(secrets.choice(alphabet) for _ in range(4))
    part2 = "".join(secrets.choice(alphabet) for _ in range(4))
    return f"PPB-{part1}-{part2}"
