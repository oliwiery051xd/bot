"""Telegram transport honoring the host's HTTPS proxy and CA configuration."""
import ssl

from aiohttp import ClientSession, TCPConnector
from aiogram.client.session.aiohttp import AiohttpSession


class TelegramSession(AiohttpSession):
    async def create_session(self) -> ClientSession:
        if self._session is None or self._session.closed:
            self._session = ClientSession(
                connector=TCPConnector(ssl=ssl.create_default_context(), limit=100),
                trust_env=True,
                headers={"User-Agent": "MONSTAL/2.0"},
            )
        return self._session
