"""The mailer port and its two implementations (SSC-049, decision 016).

``SmtpMailer`` speaks plain SMTP over the standard library and refuses to send over anything
but TLS: implicit TLS, or STARTTLS before any login (a server that does not offer it is an
error, never a fallback to plaintext). The blocking send runs in a thread. ``LogMailer`` keeps
the last messages in memory and logs only the subject; it is the default in development and
tests and is refused elsewhere (``worker.refuse_fakes``). Nothing here logs an address, a body
or the password.
"""

import asyncio
import logging
import smtplib
import ssl
from collections import deque
from dataclasses import dataclass, field
from email.message import EmailMessage
from typing import Final, Literal, Protocol

log = logging.getLogger(__name__)

Security = Literal["starttls", "tls"]
SMTP_TIMEOUT: Final = 15.0


@dataclass(frozen=True, slots=True, kw_only=True)
class Mail:
    """One plain-text message to one address."""

    to: str
    subject: str
    body: str


class Mailer(Protocol):
    async def send(self, mail: Mail) -> None:
        """Deliver ``mail`` or raise; the caller retries."""
        ...


@dataclass(frozen=True, slots=True, kw_only=True)
class SmtpConfig:
    host: str
    port: int
    security: Security
    username: str
    password: str = field(repr=False)
    sender: str


class SmtpMailer(Mailer):
    def __init__(self, config: SmtpConfig) -> None:
        self._config = config

    async def send(self, mail: Mail) -> None:
        """Send over TLS in a worker thread. Raises ``smtplib.SMTPException`` or ``OSError``."""
        await asyncio.to_thread(self._send, mail)

    def _send(self, mail: Mail) -> None:
        c = self._config
        message = EmailMessage()
        message["From"] = c.sender
        message["To"] = mail.to
        message["Subject"] = mail.subject
        message.set_content(mail.body)
        context = ssl.create_default_context()
        if c.security == "tls":
            client = smtplib.SMTP_SSL(c.host, c.port, timeout=SMTP_TIMEOUT, context=context)
        else:
            client = smtplib.SMTP(c.host, c.port, timeout=SMTP_TIMEOUT)
        with client:
            if c.security == "starttls":
                client.starttls(context=context)
            client.login(c.username, c.password)
            client.send_message(message)


class LogMailer(Mailer):
    """Delivers nothing; ``sent`` holds the last 100 messages for a test or a developer."""

    def __init__(self) -> None:
        self.sent: deque[Mail] = deque(maxlen=100)

    async def send(self, mail: Mail) -> None:
        self.sent.append(mail)
        log.info("mail not delivered (log mailer)", extra={"subject": mail.subject})
