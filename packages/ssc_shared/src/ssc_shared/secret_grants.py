"""The secret intake's write grant (SSC-026): what the control plane mints and the intake checks.

A grant is a Google ID token of the control plane's service account whose audience is the one
upload URL it is for: the intake's origin, the secret's id and a fresh nonce. So it is good for
exactly one secret, for ``GRANT_SECONDS`` after it was minted, and for nothing else that checks
an audience. The value goes from the command line to the intake; never to the control plane.
"""

import re
from typing import Final

INTAKE_PATH: Final = "/v1/secrets"
GRANT_SECONDS: Final = 600
MAX_VALUE_BYTES: Final = 64 * 1024
"""Secret Manager's own limit on one version."""
NONCE: Final = re.compile(r"[A-Za-z0-9_-]{22,64}")


def upload_url(origin: str, secret: str, nonce: str) -> str:
    """Where the value is PUT, and the audience of the grant that allows it."""
    if NONCE.fullmatch(nonce) is None:
        raise ValueError("not a grant nonce")
    return f"{origin.rstrip('/')}{INTAKE_PATH}/{secret}?grant={nonce}"
