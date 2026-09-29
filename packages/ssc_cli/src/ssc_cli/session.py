"""Per-invocation state shared by every command: the API address and a way to build a client.

Tests put a :class:`Session` with a fake transport in the Typer context object.
"""

import time
from dataclasses import dataclass

import httpx2

from ssc_cli.api import ApiClient, Sleep
from ssc_cli.config import Config, load_config
from ssc_cli.credentials import read_token


@dataclass(slots=True)
class Session:
    api_override: str | None = None
    transport: httpx2.BaseTransport | None = None
    sleep: Sleep = time.sleep

    def config(self) -> Config:
        return load_config(self.api_override)

    def client(self, token: str | None = None) -> ApiClient:
        api_url = self.config().api_url
        return ApiClient(
            api_url,
            token if token is not None else read_token(api_url),
            transport=self.transport,
            sleep=self.sleep,
        )
