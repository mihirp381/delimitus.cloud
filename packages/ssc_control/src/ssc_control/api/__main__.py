"""``python -m ssc_control.api``: the API on ``$PORT`` (8080), with ``Settings.from_env()``."""

import os
import sys

import uvicorn

from ssc_control.api.app import create_app
from ssc_control.api.settings import Settings


def main() -> int:
    uvicorn.run(
        create_app(Settings.from_env()),
        host="0.0.0.0",  # noqa: S104  (a container listens on all)
        port=int(os.environ.get("PORT", "8080")),
        proxy_headers=True,
        log_level="info",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
