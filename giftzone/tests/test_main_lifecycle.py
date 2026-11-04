"""Startup/shutdown honors automatic rate worker without contacting Telegram."""
import asyncio

from app import main
from app.services.rate_cache import RateCache
from test_auto_payments import auto_shop


async def test_polling_starts_rate_worker_sets_name_and_cancels_on_shutdown(auto_shop, monkeypatch):
    service, cache, provider, clock = auto_shop
    config = service.settings.model_copy(update={'bot_token': '123456:TEST_ONLY_FAKE_TOKEN'})
    started, cancelled = asyncio.Event(), asyncio.Event()
    calls = []

    async def fake_worker(self, stop_event):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    class FakeSession:
        async def close(self):
            calls.append('closed')

    class FakeBot:
        session = FakeSession()

        async def delete_webhook(self, **kwargs):
            calls.append('delete_webhook')

        async def set_my_name(self, name):
            calls.append(name)

    class FakeDispatcher:
        def __init__(self, **kwargs):
            self.storage = kwargs['storage']

        def include_router(self, router):
            assert router.name == 'monstal'

        def resolve_used_update_types(self):
            return ['message', 'callback_query']

        async def start_polling(self, bot, **kwargs):
            await asyncio.wait_for(started.wait(), 2)
            await self.storage.close()
            calls.append('polling_finished')

    monkeypatch.setattr(main, 'Settings', lambda: config)
    monkeypatch.setattr(main, 'Database', lambda url: service.db)
    monkeypatch.setattr(main, 'Bot', lambda *args, **kwargs: FakeBot())
    monkeypatch.setattr(main, 'Dispatcher', FakeDispatcher)
    monkeypatch.setattr(RateCache, 'run', fake_worker)
    assert await main.run() == 0
    assert started.is_set() and cancelled.is_set()
    assert calls == ['delete_webhook', 'MONSTAL', 'polling_finished', 'closed']
    assert provider.calls == 0
