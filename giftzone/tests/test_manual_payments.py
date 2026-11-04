"""Real-mode bookkeeping with fictional test fixtures, never network transfers."""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from app.config import Settings
from app.database import Database
from app.models import AdminAuditLog, BotSettings, DemoTopUp
from app.services.shop import DomainError, ShopService
from conftest import ADMIN, ALICE, BOB


@pytest_asyncio.fixture
async def real_shop(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path / 'real-bookkeeping.db'}"
    db = Database(url)
    await db.create_schema()
    config = Settings(
        _env_file=None, demo_mode=False, crypto_auto_rates=False, admin_ids=[ADMIN], database_url=url,
        merchant_telegram_username="monstal_owner",
        crypto_eth_address="0x" + "1" * 40,
        crypto_usdc_address="0x" + "2" * 40,
    )
    service = ShopService(db, config)
    await service.seed()
    await service.register(ALICE, "alice", "Alicja")
    yield service
    await db.dispose()


async def submitted_blik(service, key="manual-blik-request"):
    request = await service.create_topup(ALICE, 2550, "blik", key)
    assert request.status == "awaiting_payment"
    return await service.submit_topup(ALICE, request.id)


async def test_manual_blik_requires_admin_verification_to_credit(real_shop):
    request = await submitted_blik(real_shop)
    assert request.status == "submitted"
    assert (await real_shop.profile(ALICE)).balance_grosze == 0
    with pytest.raises(DomainError):
        await real_shop.admin_topup(ALICE, request.id, True, "receipt-2026-100")
    with pytest.raises(DomainError):
        await real_shop.admin_topup(ADMIN, request.id, True)
    approved = await real_shop.admin_topup(ADMIN, request.id, True, "receipt-2026-100")
    assert approved.status == "approved"
    assert approved.confirmation_reference == "BLIK:receipt-2026-100"
    assert (await real_shop.profile(ALICE)).balance_grosze == 2550
    assert len(await real_shop.transactions(ALICE)) == 1
    async with real_shop.db.sessions() as session:
        assert await session.scalar(select(AdminAuditLog.id).where(
            AdminAuditLog.target == request.id))


async def test_concurrent_manual_approvals_credit_once_and_bind_reference(real_shop):
    request = await submitted_blik(real_shop)
    results = await asyncio.gather(*(
        real_shop.admin_topup(ADMIN, request.id, True, "receipt-2026-200")
        for _ in range(4)
    ))
    assert all(result.status == "approved" for result in results)
    assert (await real_shop.profile(ALICE)).balance_grosze == 2550
    assert len(await real_shop.transactions(ALICE)) == 1
    with pytest.raises(DomainError):
        await real_shop.admin_topup(ADMIN, request.id, True, "different-receipt")


async def test_same_receipt_cannot_credit_two_payment_requests(real_shop):
    first = await submitted_blik(real_shop, "receipt-first-request")
    second = await submitted_blik(real_shop, "receipt-second-request")
    await real_shop.admin_topup(ADMIN, first.id, True, "same-bank-receipt")
    with pytest.raises(DomainError):
        await real_shop.admin_topup(ADMIN, second.id, True, "same-bank-receipt")
    assert (await real_shop.topup(ALICE, second.id)).status == "submitted"
    assert (await real_shop.profile(ALICE)).balance_grosze == 2550


async def test_blik_code_is_not_a_payment_receipt(real_shop):
    request = await submitted_blik(real_shop)
    with pytest.raises(DomainError):
        await real_shop.admin_topup(ADMIN, request.id, True, "123456")
    assert (await real_shop.profile(ALICE)).balance_grosze == 0


async def test_crypto_requires_quote_then_user_submission_then_manual_approval(real_shop):
    request = await real_shop.create_topup(ALICE, 10000, "crypto", "crypto-real-request", asset="USDC")
    assert request.status == "awaiting_quote"
    assert request.asset == "USDC"
    assert "Ethereum" in request.network
    assert request.wallet_address == real_shop.settings.crypto_usdc_address
    with pytest.raises(DomainError):
        await real_shop.submit_topup(ALICE, request.id)
    quoted = await real_shop.admin_quote_topup(ADMIN, request.id, "25.123456")
    assert quoted.status == "awaiting_payment"
    assert quoted.crypto_amount == "25.123456"
    assert quoted.quote_expires_at is not None
    tx_hash = "0x" + "c" * 64
    with pytest.raises(DomainError):
        await real_shop.admin_topup(ADMIN, request.id, True, tx_hash)
    submitted = await real_shop.submit_topup(ALICE, request.id, quote_token=real_shop.quote_token(quoted))
    assert submitted.status == "submitted"
    assert (await real_shop.profile(ALICE)).balance_grosze == 0
    approved = await real_shop.admin_topup(ADMIN, request.id, True, tx_hash)
    assert approved.status == "approved"
    assert (await real_shop.profile(ALICE)).balance_grosze == 10000


async def test_quote_expiration_requires_new_quote_before_submission(real_shop):
    request = await real_shop.create_topup(ALICE, 1000, "crypto", "expired-crypto-request", asset="ETH")
    first_quote = await real_shop.admin_quote_topup(ADMIN, request.id, "0.0000001")
    original_token = real_shop.quote_token(first_quote)
    async with real_shop.db.write() as session:
        stored = await session.get(DemoTopUp, request.id)
        stored.quote_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    with pytest.raises(DomainError):
        expired = await real_shop.topup(ALICE, request.id)
        await real_shop.submit_topup(ALICE, request.id, quote_token=real_shop.quote_token(expired))
    updated = await real_shop.admin_quote_topup(ADMIN, request.id, "0.0000002")
    assert updated.crypto_amount == "0.0000002"
    with pytest.raises(DomainError):
        await real_shop.submit_topup(ALICE, request.id, quote_token=original_token)
    assert (await real_shop.profile(ALICE)).balance_grosze == 0
    assert (await real_shop.submit_topup(ALICE, request.id, quote_token=real_shop.quote_token(updated))).status == "submitted"


@pytest.mark.parametrize("value", ["0", "-1", "NaN", "Infinity", "0.0000001", "1e1000000"])
async def test_crypto_quote_rejects_invalid_or_excess_precision(real_shop, value):
    request = await real_shop.create_topup(ALICE, 1000, "crypto", "invalid-crypto-quote", asset="USDC")
    with pytest.raises(DomainError):
        await real_shop.admin_quote_topup(ADMIN, request.id, value)
    assert (await real_shop.topup(ALICE, request.id)).status == "awaiting_quote"


async def test_user_cannot_submit_another_users_payment(real_shop):
    request = await real_shop.create_topup(ALICE, 1000, "blik", "payment-owner-key")
    await real_shop.register(BOB)
    with pytest.raises(DomainError):
        await real_shop.submit_topup(BOB, request.id)
    assert (await real_shop.topup(ALICE, request.id)).status == "awaiting_payment"


async def test_user_submission_is_idempotent_and_never_a_credit(real_shop):
    request = await real_shop.create_topup(ALICE, 1000, "blik", "payment-submit-twice")
    for _ in range(3):
        assert (await real_shop.submit_topup(ALICE, request.id)).status == "submitted"
    assert await real_shop.transactions(ALICE) == []
    assert (await real_shop.profile(ALICE)).balance_grosze == 0


async def test_missing_contact_or_wallet_prevents_real_payment_creation(real_shop):
    empty = Settings(_env_file=None, demo_mode=False, admin_ids=[ADMIN], database_url=real_shop.settings.database_url)
    service = ShopService(real_shop.db, empty)
    with pytest.raises(DomainError):
        await service.create_topup(ALICE, 1000, "blik", "missing-owner-contact")
    with pytest.raises(DomainError):
        await service.create_topup(ALICE, 1000, "crypto", "missing-btc-wallet", asset="BTC")
    with pytest.raises(DomainError):
        await real_shop.create_topup(ALICE, 1000, "crypto", "unsupported-currency", asset="DOGE")
    assert await real_shop.topups(ALICE) == []


async def test_real_mode_does_not_seed_demo_products_or_allow_spending(real_shop):
    assert await real_shop.categories() == []
    await real_shop.admin_balance(ADMIN, ALICE, 10000, "manual-credit-real")
    with pytest.raises(DomainError):
        await real_shop.purchase(ALICE, 1, "no-demo-product-charge")
    assert (await real_shop.profile(ALICE)).balance_grosze == 10000
    assert await real_shop.orders(ALICE) == []


async def test_real_and_demo_databases_cannot_be_mixed(real_shop):
    demo_config = Settings(_env_file=None, demo_mode=True, admin_ids=[ADMIN], database_url=real_shop.settings.database_url)
    demo_service = ShopService(real_shop.db, demo_config)
    with pytest.raises(DomainError):
        await demo_service.seed()
    with pytest.raises(DomainError):
        await demo_service.admin_balance(ADMIN, ALICE, 1000, "mode-mix-attempt")
    assert (await real_shop.profile(ALICE)).balance_grosze == 0


async def test_legacy_demo_balance_is_never_reinterpreted_as_real(shop, buyer):
    await shop.admin_balance(ADMIN, ALICE, 10000, "legacy-demo-credit")
    async with shop.db.write() as session:
        await session.execute(delete(BotSettings).where(BotSettings.key == "balance_mode"))
    real_config = Settings(_env_file=None, demo_mode=False, admin_ids=[ADMIN], database_url=shop.settings.database_url)
    real_service = ShopService(shop.db, real_config)
    with pytest.raises(DomainError):
        await real_service.seed()
    # Original DEMO balance remains unchanged and usable in its original mode.
    assert (await shop.profile(ALICE)).balance_grosze == 10000


async def test_rejected_real_request_is_never_credited(real_shop):
    request = await real_shop.create_topup(ALICE, 1000, "crypto", "reject-real-payment", asset="ETH")
    assert (await real_shop.admin_topup(ADMIN, request.id, False)).status == "rejected"
    with pytest.raises(DomainError):
        await real_shop.admin_topup(ADMIN, request.id, True, "Ethereum:rejected-tx")
    assert (await real_shop.profile(ALICE)).balance_grosze == 0


async def test_manual_credit_correction_keeps_audit_and_idempotency(real_shop):
    await real_shop.admin_balance(ADMIN, ALICE, 2500, "cash-manual-credit")
    await real_shop.admin_balance(ADMIN, ALICE, 2500, "cash-manual-credit")
    await real_shop.admin_balance(ADMIN, ALICE, -500, "cash-manual-correction")
    assert (await real_shop.profile(ALICE)).balance_grosze == 2000
    assert len(await real_shop.transactions(ALICE)) == 2


async def test_ethereum_and_usdc_cannot_reuse_same_chain_transaction(real_shop):
    requests = []
    for asset in ("ETH", "USDC"):
        request = await real_shop.create_topup(ALICE, 1000, "crypto", f"same-chain-{asset}", asset=asset)
        quote = await real_shop.admin_quote_topup(ADMIN, request.id, "1")
        await real_shop.submit_topup(ALICE, request.id, quote_token=real_shop.quote_token(quote))
        requests.append(request)
    tx_hash = "0x" + "a" * 64
    await real_shop.admin_topup(ADMIN, requests[0].id, True, tx_hash)
    with pytest.raises(DomainError):
        await real_shop.admin_topup(ADMIN, requests[1].id, True, tx_hash)
    assert (await real_shop.profile(ALICE)).balance_grosze == 1000


async def test_real_ui_blik_contact_and_confirmed_manual_credit(real_shop):
    from test_ui import button, feed, rendered, setup_ui

    dp, bot, session = setup_ui(real_shop)
    try:
        await feed(dp, bot, "/start")
        assert "MONSTAL" in rendered(session).text
        await feed(dp, bot, "topups", callback=True)
        await feed(dp, bot, "topup_method:blik", callback=True)
        assert "@monstal_owner" in rendered(session).text
        await feed(dp, bot, "manual_amount:BLIK:1000", callback=True)
        await feed(dp, bot, button(session, "manual_create:"), callback=True)
        request = (await real_shop.topups(ALICE))[0]
        await feed(dp, bot, f"topup_status:{request.id}", callback=True)
        await feed(dp, bot, button(session, "topup_submit:"), callback=True)
        assert (await real_shop.profile(ALICE)).balance_grosze == 0
        await feed(dp, bot, "/admin", user_id=ADMIN)
        await feed(dp, bot, f"a:topup:{request.id}", user_id=ADMIN, callback=True)
        await feed(dp, bot, f"a:tapprove:{request.id}", user_id=ADMIN, callback=True)
        await feed(dp, bot, "manual-ui-receipt-100", user_id=ADMIN)
        execute = button(session, "a:execute:")
        assert (await real_shop.profile(ALICE)).balance_grosze == 0
        await feed(dp, bot, execute, user_id=ADMIN, callback=True)
        assert (await real_shop.profile(ALICE)).balance_grosze == 1000
    finally:
        await dp.storage.close()
        await bot.session.close()



@pytest.mark.parametrize("asset", ["LTC", "XMR"])
async def test_ltc_and_xmr_real_deposits_require_private_contact(real_shop, asset):
    with pytest.raises(DomainError, match="prywatnie"):
        await real_shop.create_topup(ALICE, 1000, "crypto", f"private-contact-{asset}", asset=asset)
    assert await real_shop.topups(ALICE) == []


async def test_real_ui_crypto_quote_amount_network_address_and_approval(real_shop):
    from test_ui import button, feed, rendered, setup_ui

    dp, bot, session = setup_ui(real_shop)
    try:
        await feed(dp, bot, "/start")
        await feed(dp, bot, "topup_method:crypto", callback=True)
        methods = [item.callback_data for row in rendered(session).reply_markup.inline_keyboard for item in row]
        assert set(f"crypto_asset:{asset}" for asset in ("BTC", "ETH", "USDC", "SOL", "LTC", "XMR")) <= set(methods)
        await feed(dp, bot, "crypto_asset:LTC", callback=True)
        assert "@monstal_owner" in rendered(session).text
        assert "prywatnie" in rendered(session).text
        assert "Adres odbiorcy" not in rendered(session).text
        await feed(dp, bot, "crypto_asset:XMR", callback=True)
        assert "@monstal_owner" in rendered(session).text
        assert "prywatnie" in rendered(session).text
        assert "Adres odbiorcy" not in rendered(session).text
        await feed(dp, bot, "crypto_asset:USDC", callback=True)
        await feed(dp, bot, "manual_amount:USDC:2500", callback=True)
        await feed(dp, bot, button(session, "manual_create:"), callback=True)
        request = (await real_shop.topups(ALICE))[0]
        await feed(dp, bot, "/admin", user_id=ADMIN)
        await feed(dp, bot, f"a:tquote:{request.id}", user_id=ADMIN, callback=True)
        await feed(dp, bot, "6.123456", user_id=ADMIN)
        await feed(dp, bot, button(session, "a:execute:"), user_id=ADMIN, callback=True)
        await feed(dp, bot, f"topup_status:{request.id}", callback=True)
        text = rendered(session).text
        assert "6.123456" in text
        assert "USDC" in text
        assert "Ethereum" in text
        assert real_shop.settings.crypto_usdc_address in text
        await feed(dp, bot, button(session, "topup_submit:"), callback=True)
        assert (await real_shop.profile(ALICE)).balance_grosze == 0
        await feed(dp, bot, f"a:tapprove:{request.id}", user_id=ADMIN, callback=True)
        await feed(dp, bot, "0x" + "b" * 64, user_id=ADMIN)
        await feed(dp, bot, button(session, "a:execute:"), user_id=ADMIN, callback=True)
        assert (await real_shop.profile(ALICE)).balance_grosze == 2500
    finally:
        await dp.storage.close()
        await bot.session.close()


async def test_active_crypto_quote_cannot_change_and_same_amount_is_idempotent(real_shop):
    request = await real_shop.create_topup(ALICE, 2500, "crypto", "fixed-active-quote", asset="USDC")
    quote = await real_shop.admin_quote_topup(ADMIN, request.id, "6.123456")
    token = real_shop.quote_token(quote)
    expiry = real_shop._utc(quote.quote_expires_at)
    with pytest.raises(DomainError):
        await real_shop.admin_quote_topup(ADMIN, request.id, "7.123456")
    repeated = await real_shop.admin_quote_topup(ADMIN, request.id, "6.123456")
    assert real_shop._utc(repeated.quote_expires_at) == expiry
    assert real_shop.quote_token(repeated) == token
    assert real_shop.quote_token(await real_shop.topup(ALICE, request.id)) == token
    assert (await real_shop.topup(ALICE, request.id)).crypto_amount == "6.123456"


async def test_crypto_submission_requires_exact_quote_version(real_shop):
    request = await real_shop.create_topup(ALICE, 2500, "crypto", "quote-version-bound", asset="USDC")
    quote = await real_shop.admin_quote_topup(ADMIN, request.id, "6.123456")
    for token in (None, "", "wrong-token"):
        with pytest.raises(DomainError):
            await real_shop.submit_topup(ALICE, request.id, quote_token=token)
    assert (await real_shop.topup(ALICE, request.id)).status == "awaiting_payment"
    assert (await real_shop.profile(ALICE)).balance_grosze == 0
    assert (await real_shop.submit_topup(ALICE, request.id, quote_token=real_shop.quote_token(quote))).status == "submitted"
