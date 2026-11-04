"""Automatic quote integration uses fictional snapshots and no external APIs."""
import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
import pytest_asyncio

from app.config import Settings
from app.database import Database
from app.models import BotSettings, DemoTopUp
from app.services.manual_payments import CRYPTO_ASSETS
from app.services.rate_cache import CACHE_KEY, RateCache
from app.services.rates import RateSnapshot, RateUnavailable
from app.services.shop import DomainError, ShopService
from conftest import ADMIN, ALICE, BOB


class Clock:
    def __init__(self):
        self.now = datetime.now(timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


class FakeRates:
    def __init__(self, clock):
        self.clock = clock
        self.calls = 0
        self.fail = False
        self.price = Decimal("10000")
        self.low = Decimal("8000")
        self.timestamp_offset = timedelta(0)
        self.omit_asset = None
        self.on_fetch = None

    async def fetch(self):
        self.calls += 1
        await asyncio.sleep(0.01)
        if self.on_fetch:
            await self.on_fetch()
        if self.fail:
            raise RateUnavailable("Testowe źródło niedostępne")
        now = self.clock() + self.timestamp_offset
        return {
            asset: RateSnapshot(asset=asset, price_pln=self.price, low_24h=self.low,
                                source="Test CoinGecko PLN", source_updated_at=now, fetched_at=now)
            for asset in CRYPTO_ASSETS if asset != self.omit_asset
        }


@pytest_asyncio.fixture
async def auto_shop(tmp_path):
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'auto-quotes.db'}")
    await db.create_schema()
    config = Settings(_env_file=None, demo_mode=False, crypto_auto_rates=True,
                      admin_ids=[ADMIN], merchant_telegram_username="monstal_owner",
                      crypto_eth_address="0x" + "1" * 40,
                      crypto_usdc_address="0x" + "2" * 40,
                      rate_refresh_seconds=300, rate_max_age_seconds=3600)
    clock = Clock()
    provider = FakeRates(clock)
    cache = RateCache(db, config, provider=provider, clock=clock)
    service = ShopService(db, config, rate_cache=cache)
    await service.seed()
    await service.register(ALICE, "alice", "Alicja")
    yield service, cache, provider, clock
    await db.dispose()


async def test_fifty_pln_creates_exact_frozen_eth_quote_without_manual_pricing(auto_shop):
    service, cache, provider, clock = auto_shop
    request = await service.create_topup(ALICE, 5000, "crypto", "automatic-eth-fifty", asset="ETH")
    assert request.status == "awaiting_payment"
    assert Decimal(request.crypto_amount) == Decimal("0.005")
    assert Decimal(request.rate_pln) == Decimal("10000")
    assert request.rate_source == "Test CoinGecko PLN"
    assert request.rate_mode == "current"
    assert service._utc(request.rate_fetched_at) == clock.now
    assert service._utc(request.rate_source_updated_at) == clock.now
    assert request.wallet_address == service.settings.crypto_eth_address
    assert request.network == "Ethereum mainnet"
    assert (await service.profile(ALICE)).balance_grosze == 0
    with pytest.raises(DomainError):
        await service.admin_quote_topup(ADMIN, request.id, "0.01")
    await service.submit_topup(ALICE, request.id, quote_token=service.quote_token(request))
    assert (await service.profile(ALICE)).balance_grosze == 0
    await service.admin_topup(ADMIN, request.id, True, "0x" + "a" * 64)
    await service.admin_topup(ADMIN, request.id, True, "0x" + "a" * 64)
    assert (await service.profile(ALICE)).balance_grosze == 5000
    assert len(await service.transactions(ALICE)) == 1


async def test_rounding_never_requests_less_crypto_than_target_pln(auto_shop):
    service, cache, provider, clock = auto_shop
    provider.price = Decimal("7777")
    request = await service.create_topup(ALICE, 5000, "crypto", "rounded-eth-amount", asset="ETH")
    crypto = Decimal(request.crypto_amount)
    quantum = Decimal("0.000000000000000001")
    assert crypto * provider.price >= Decimal("50")
    assert (crypto - quantum) * provider.price < Decimal("50")


async def test_current_and_24_hour_low_are_explicitly_different(auto_shop):
    service, cache, provider, clock = auto_shop
    current = await service.create_topup(ALICE, 5000, "crypto", "current-rate-quote", asset="ETH")
    low_settings = service.settings.model_copy(update={"rate_price_mode": "low_24h"})
    low_service = ShopService(service.db, low_settings, rate_cache=cache)
    low = await low_service.create_topup(ALICE, 5000, "crypto", "daily-low-quote", asset="ETH")
    assert Decimal(current.crypto_amount) == Decimal("0.005")
    assert Decimal(low.crypto_amount) == Decimal("0.00625")
    assert low.rate_mode == "low_24h"
    assert Decimal(low.rate_pln) == Decimal("8000")
    assert provider.calls == 1


async def test_concurrent_quotes_coalesce_refresh_without_duplicate_requests(auto_shop):
    service, cache, provider, clock = auto_shop
    requests = await asyncio.gather(*(
        service.create_topup(ALICE, 5000, "crypto", f"parallel-quote-{index}", asset="ETH")
        for index in range(6)
    ))
    assert len({request.id for request in requests}) == 6
    assert all(Decimal(request.crypto_amount) == Decimal("0.005") for request in requests)
    assert provider.calls == 1
    assert (await service.profile(ALICE)).balance_grosze == 0


async def test_cached_snapshots_survive_restart_without_network(auto_shop):
    service, cache, provider, clock = auto_shop
    await cache.get("ETH")
    unavailable = FakeRates(clock)
    unavailable.fail = True
    restarted = RateCache(service.db, service.settings, provider=unavailable, clock=clock)
    snapshot = await restarted.get("ETH")
    assert snapshot.price_pln == Decimal("10000")
    assert unavailable.calls == 0


async def test_failed_refresh_uses_fresh_cache_then_rejects_expired_cache(auto_shop):
    service, cache, provider, clock = auto_shop
    first = await cache.get("ETH")
    provider.fail = True
    clock.advance(service.settings.rate_refresh_seconds + 1)
    assert await cache.get("ETH") == first
    calls_after_failure = provider.calls
    assert await cache.get("USDC")
    assert provider.calls == calls_after_failure  # Failed APIs are not hammered.
    clock.advance(service.settings.rate_max_age_seconds + 1)
    with pytest.raises(RateUnavailable):
        await cache.get("ETH")


async def test_initial_api_failure_keeps_request_pending_without_payment_instruction(auto_shop):
    service, cache, provider, clock = auto_shop
    provider.fail = True
    request = await service.create_topup(ALICE, 5000, "crypto", "unavailable-first-quote", asset="ETH")
    assert request.status == "awaiting_quote"
    assert request.crypto_amount is None
    assert request.quote_expires_at is None
    with pytest.raises(DomainError):
        await service.auto_quote_topup(ALICE, request.id)
    assert (await service.profile(ALICE)).balance_grosze == 0


@pytest.mark.parametrize("raw", ["{broken JSON", '{"version":99,"snapshots":{}}'])
async def test_corrupt_persistent_cache_cannot_create_payment_quote(auto_shop, raw):
    service, cache, provider, clock = auto_shop
    async with service.db.write() as session:
        session.add(BotSettings(key=CACHE_KEY, value=raw))
    provider.fail = True
    broken_cache = RateCache(service.db, service.settings, provider=provider, clock=clock)
    with pytest.raises(RateUnavailable):
        await broken_cache.get("ETH")


@pytest.mark.parametrize("timestamp_offset", [timedelta(days=-2), timedelta(hours=1)])
async def test_stale_or_future_provider_snapshots_never_become_usable(auto_shop, timestamp_offset):
    service, cache, provider, clock = auto_shop
    provider.timestamp_offset = timestamp_offset
    with pytest.raises(RateUnavailable):
        await cache.get("ETH")
    async with service.db.sessions() as session:
        assert await session.get(BotSettings, CACHE_KEY) is None


async def test_incomplete_refresh_preserves_valid_complete_cache(auto_shop):
    service, cache, provider, clock = auto_shop
    first = await cache.get("ETH")
    provider.omit_asset = "XMR"
    provider.price = Decimal("99999")
    clock.advance(service.settings.rate_refresh_seconds + 1)
    current = await cache.get("ETH")
    assert current == first
    restarted = RateCache(service.db, service.settings, provider=provider, clock=clock)
    provider.fail = True
    assert await restarted.get("ETH") == first


async def test_expired_quote_reprices_only_owner_and_rejects_previous_quote_token(auto_shop):
    service, cache, provider, clock = auto_shop
    request = await service.create_topup(ALICE, 5000, "crypto", "expired-auto-quote", asset="ETH")
    first_token = service.quote_token(request)
    await service.register(BOB)
    with pytest.raises(DomainError):
        await service.auto_quote_topup(BOB, request.id)
    async with service.db.write() as session:
        stored = await session.get(DemoTopUp, request.id)
        stored.quote_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    clock.advance(service.settings.rate_refresh_seconds + 1)
    provider.price = Decimal("20000")
    new_quote = await service.auto_quote_topup(ALICE, request.id)
    assert Decimal(new_quote.crypto_amount) == Decimal("0.0025")
    assert service.quote_token(new_quote) != first_token
    with pytest.raises(DomainError):
        await service.submit_topup(ALICE, request.id, quote_token=first_token)
    await service.submit_topup(ALICE, request.id, quote_token=service.quote_token(new_quote))
    assert (await service.profile(ALICE)).balance_grosze == 0


async def test_live_quote_remains_frozen_when_market_changes(auto_shop):
    service, cache, provider, clock = auto_shop
    request = await service.create_topup(ALICE, 5000, "crypto", "frozen-auto-quote", asset="ETH")
    token = service.quote_token(request)
    expiry = service._utc(request.quote_expires_at)
    provider.price = Decimal("5000")
    await cache.refresh(force=True)
    repeated = await service.auto_quote_topup(ALICE, request.id)
    assert Decimal(repeated.crypto_amount) == Decimal("0.005")
    assert service.quote_token(repeated) == token
    assert service._utc(repeated.quote_expires_at) == expiry


async def test_provider_is_awaited_outside_database_write_transaction(auto_shop):
    service, cache, provider, clock = auto_shop
    async def probe_database_write():
        async with service.db.write() as session:
            session.add(BotSettings(key="rate_fetch_probe", value="outside-business-write"))
    provider.on_fetch = probe_database_write
    request = await asyncio.wait_for(service.create_topup(
        ALICE, 5000, "crypto", "provider-outside-transaction", asset="ETH"), timeout=5)
    assert request.status == "awaiting_payment"


async def test_quote_expiry_cannot_outlive_the_selected_rate_snapshot(auto_shop):
    service, cache, provider, clock = auto_shop
    provider.timestamp_offset = -timedelta(seconds=service.settings.rate_max_age_seconds - 30)
    request = await service.create_topup(ALICE, 5000, "crypto", "quote-rate-expiry-cap", asset="ETH")
    assert request.status == "awaiting_payment"
    remaining = (service._utc(request.quote_expires_at) - datetime.now(timezone.utc)).total_seconds()
    assert 0 < remaining <= 31
    assert service._utc(request.quote_expires_at) <= (
        service._utc(request.rate_source_updated_at) + timedelta(seconds=service.settings.rate_max_age_seconds))


@pytest.mark.parametrize("field", ["source_updated_at", "fetched_at"])
async def test_each_timestamp_independently_prevents_stale_snapshot_reuse(auto_shop, field):
    service, cache, provider, clock = auto_shop
    original_fetch = provider.fetch
    async def partially_stale_fetch():
        batch = await original_fetch()
        batch["ETH"] = replace(batch["ETH"], **{field: clock.now - timedelta(days=2)})
        return batch
    provider.fetch = partially_stale_fetch
    with pytest.raises(RateUnavailable):
        await cache.get("ETH")


async def test_user_ui_shows_calculated_eth_amount_source_and_address_immediately(auto_shop):
    from test_ui import button, feed, rendered, setup_ui
    service, cache, provider, clock = auto_shop
    dp, bot, telegram = setup_ui(service)
    try:
        for value, is_callback in (("/start", False), ("topup_method:crypto", True),
                                   ("crypto_asset:ETH", True), ("manual_amount:ETH:5000", True)):
            await feed(dp, bot, value, callback=is_callback)
        await feed(dp, bot, button(telegram, "manual_create:"), callback=True)
        request = (await service.topups(ALICE))[0]
        await feed(dp, bot, f"topup_status:{request.id}", callback=True)
        screen = rendered(telegram)
        assert "0.005000000000000000" in screen.text
        assert "10000" in screen.text
        assert "Test CoinGecko PLN" in screen.text
        assert "Aktualizacja źródła" in screen.text
        assert service.settings.crypto_eth_address in screen.text
        assert "Ethereum mainnet" in screen.text
        submit = button(telegram, "topup_submit:")
        assert len(submit.encode()) <= 64
        await feed(dp, bot, submit, callback=True)
        assert (await service.profile(ALICE)).balance_grosze == 0
        await feed(dp, bot, f"a:topup:{request.id}", user_id=ADMIN, callback=True)
        admin_buttons = [item.callback_data for row in rendered(telegram).reply_markup.inline_keyboard for item in row]
        assert not any(value.startswith("a:tquote:") for value in admin_buttons if value)
        await feed(dp, bot, f"a:tapprove:{request.id}", user_id=ADMIN, callback=True)
        await feed(dp, bot, "0x" + "d" * 64, user_id=ADMIN)
        await feed(dp, bot, button(telegram, "a:execute:"), user_id=ADMIN, callback=True)
        assert (await service.profile(ALICE)).balance_grosze == 5000
    finally:
        await dp.storage.close()
        await bot.session.close()


async def test_user_ui_does_not_show_transfer_address_when_rates_are_unavailable(auto_shop):
    from test_ui import feed, rendered, setup_ui
    service, cache, provider, clock = auto_shop
    provider.fail = True
    request = await service.create_topup(ALICE, 5000, "crypto", "ui-unavailable-auto-rates", asset="ETH")
    dp, bot, telegram = setup_ui(service)
    try:
        await feed(dp, bot, f"topup_status:{request.id}", callback=True)
        screen = rendered(telegram)
        assert service.settings.crypto_eth_address not in screen.text
        buttons = [item.callback_data for row in screen.reply_markup.inline_keyboard for item in row]
        assert not any(value.startswith("topup_submit:") for value in buttons if value)
        assert (await service.topup(ALICE, request.id)).status == "awaiting_quote"
        assert (await service.profile(ALICE)).balance_grosze == 0
    finally:
        await dp.storage.close()
        await bot.session.close()
