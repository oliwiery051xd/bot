import ssl

from app.utils.session import TelegramSession


async def test_transport_honors_environment_and_preserves_tls_verification():
    session = TelegramSession()
    try:
        client = await session.create_session()
        assert client.trust_env is True
        assert client.connector._ssl.verify_mode == ssl.CERT_REQUIRED
        assert client.connector._ssl.check_hostname is True
        assert await session.create_session() is client
    finally:
        await session.close()
