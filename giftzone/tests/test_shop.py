import asyncio

import pytest
from sqlalchemy import func, select

from app.models import AdminAuditLog, Order, Referral
from app.database.core import Database
from app.services.shop import ShopService
from app.services.shop import DomainError
from conftest import ADMIN, ALICE, BOB, CHARLIE


pytestmark = pytest.mark.asyncio


async def test_registration_is_idempotent_and_updates_telegram_data(shop):
    original = await shop.register(ALICE, "alice", "Alicja")
    again = await shop.register(ALICE, "alice_new", "Ala")
    assert again.id == original.id
    assert again.telegram_id == ALICE
    assert again.username == "alice_new"
    assert again.first_name == "Ala"
    assert again.balance_grosze == 0
    assert again.bonus_grosze == 0
    assert again.status == "active"
    assert again.created_at is not None


async def test_seed_is_repeatable(shop):
    before = [(c.id, c.name) for c in await shop.categories()]
    await shop.seed()
    after = [(c.id, c.name) for c in await shop.categories()]
    assert set(before) == set(after)
    assert len(after) == 6


async def test_purchase_debits_balance_and_reserves_stock(shop, funded_buyer, product):
    order = await shop.purchase(ALICE, product.id, "purchase-first")
    assert order.status == "pending"
    assert order.price_grosze == product.price_grosze
    assert order.product_name.startswith(product.name)
    assert product.region in order.product_name
    assert order.id
    assert (await shop.profile(ALICE)).balance_grosze == 100_000 - product.price_grosze
    assert (await shop.product(product.id)).stock == product.stock - 1
    assert [o.id for o in await shop.orders(ALICE)] == [order.id]


async def test_purchase_without_balance_is_atomic(shop, buyer, product):
    with pytest.raises(DomainError):
        await shop.purchase(ALICE, product.id, "no-funds")
    assert (await shop.profile(ALICE)).balance_grosze == 0
    assert (await shop.product(product.id)).stock == product.stock
    assert await shop.orders(ALICE) == []


async def test_double_click_returns_same_order_and_single_debit(shop, funded_buyer, product):
    first = await shop.purchase(ALICE, product.id, "double-click")
    second = await shop.purchase(ALICE, product.id, "double-click")
    assert first.id == second.id
    assert len(await shop.orders(ALICE)) == 1
    assert (await shop.profile(ALICE)).balance_grosze == 100_000 - product.price_grosze
    assert (await shop.product(product.id)).stock == product.stock - 1


async def test_concurrent_duplicate_purchase_only_debits_once(shop, funded_buyer, product):
    orders = await asyncio.gather(
        *(shop.purchase(ALICE, product.id, "concurrent-key") for _ in range(5))
    )
    assert len({order.id for order in orders}) == 1
    assert (await shop.profile(ALICE)).balance_grosze == 100_000 - product.price_grosze
    assert (await shop.product(product.id)).stock == product.stock - 1


async def test_concurrent_distinct_purchases_cannot_overdraw(shop, buyer, product):
    await shop.admin_balance(ADMIN, ALICE, product.price_grosze, "one-order-funds")
    results = await asyncio.gather(
        shop.purchase(ALICE, product.id, "race-order-a"),
        shop.purchase(ALICE, product.id, "race-order-b"),
        return_exceptions=True,
    )
    assert sum(isinstance(result, DomainError) for result in results) == 1
    assert sum(isinstance(result, Order) for result in results) == 1
    assert (await shop.profile(ALICE)).balance_grosze == 0
    assert len(await shop.orders(ALICE)) == 1


async def test_purchase_key_is_bound_to_user_and_product(shop, funded_buyer, product):
    await shop.purchase(ALICE, product.id, "bound-key")
    await shop.register(BOB)
    await shop.admin_balance(ADMIN, BOB, 100_000, "bob-funds")
    with pytest.raises(DomainError):
        await shop.purchase(BOB, product.id, "bound-key")
    other = await shop.admin_product(
        ADMIN, name="Other DEMO", category_id=product.category_id,
        denomination="1", currency="PLN", region="TEST", price_grosze=100,
        description="Fikcyjny produkt", stock=2, active=True,
    )
    with pytest.raises(DomainError):
        await shop.purchase(ALICE, other.id, "bound-key")


async def test_cancel_returns_funds_and_stock_once(shop, funded_buyer, product):
    order = await shop.purchase(ALICE, product.id, "cancel-me")
    first = await shop.admin_order(ADMIN, order.id, "cancelled")
    second = await shop.admin_order(ADMIN, order.id, "cancelled")
    assert first.status == second.status == "cancelled"
    assert (await shop.profile(ALICE)).balance_grosze == 100_000
    assert (await shop.product(product.id)).stock == product.stock
    with pytest.raises(DomainError):
        await shop.admin_order(ADMIN, order.id, "completed")


async def test_completed_refund_is_idempotent(shop, funded_buyer, product):
    order = await shop.purchase(ALICE, product.id, "refund-me")
    await shop.admin_order(ADMIN, order.id, "processing")
    completed = await shop.admin_order(ADMIN, order.id, "completed")
    assert completed.demo_code.startswith("DEMO-")
    assert (await shop.profile(ALICE)).purchase_count == 1
    assert (await shop.profile(ALICE)).total_spent_grosze == product.price_grosze
    await shop.admin_order(ADMIN, order.id, "refunded")
    await shop.admin_order(ADMIN, order.id, "refunded")
    assert (await shop.profile(ALICE)).balance_grosze == 100_000
    assert (await shop.product(product.id)).stock == product.stock


async def test_referral_awarded_only_after_first_completed_purchase(shop, product):
    referrer = await shop.register(BOB)
    referred = await shop.register(ALICE, referrer_telegram_id=BOB)
    assert referred.referrer_id == referrer.id
    await shop.admin_balance(ADMIN, ALICE, 100_000, "referred-funds")
    order = await shop.purchase(ALICE, product.id, "referred-first")
    assert (await shop.profile(BOB)).bonus_grosze == 0
    assert (await shop.referral_stats(BOB))["invited"] == 1
    await shop.admin_order(ADMIN, order.id, "completed")
    profile = await shop.profile(BOB)
    assert profile.bonus_grosze == 1000
    assert profile.balance_grosze == 0
    assert profile.successful_referrals == 1
    assert (await shop.referral_stats(BOB))["earned_grosze"] == 1000


async def test_referral_not_awarded_twice_even_with_concurrent_completions(shop, product):
    await shop.register(BOB)
    await shop.register(ALICE, referrer_telegram_id=BOB)
    await shop.admin_balance(ADMIN, ALICE, 100_000, "many-orders-funds")
    first = await shop.purchase(ALICE, product.id, "first-order")
    second = await shop.purchase(ALICE, product.id, "second-order")
    await asyncio.gather(
        shop.admin_order(ADMIN, first.id, "completed"),
        shop.admin_order(ADMIN, second.id, "completed"),
        shop.admin_order(ADMIN, first.id, "completed"),
    )
    assert (await shop.profile(BOB)).bonus_grosze == 1000
    assert (await shop.profile(BOB)).successful_referrals == 1
    assert (await shop.profile(ALICE)).purchase_count == 2


async def test_referral_refund_reverses_bonus_permanently(shop, product):
    await shop.register(BOB)
    await shop.register(ALICE, referrer_telegram_id=BOB)
    await shop.admin_balance(ADMIN, ALICE, 100_000, "referral-refund-funds")
    first = await shop.purchase(ALICE, product.id, "qualifying")
    await shop.admin_order(ADMIN, first.id, "completed")
    await shop.admin_order(ADMIN, first.id, "refunded")
    await shop.admin_order(ADMIN, first.id, "refunded")
    assert (await shop.profile(BOB)).bonus_grosze == 0
    assert (await shop.profile(BOB)).successful_referrals == 0
    second = await shop.purchase(ALICE, product.id, "later-order")
    await shop.admin_order(ADMIN, second.id, "completed")
    assert (await shop.profile(BOB)).bonus_grosze == 0
    async with shop.db.sessions() as session:
        referral = await session.scalar(select(Referral))
        assert referral.status == "reversed"


async def test_cancelled_order_does_not_qualify_referral(shop, product):
    await shop.register(BOB)
    await shop.register(ALICE, referrer_telegram_id=BOB)
    await shop.admin_balance(ADMIN, ALICE, 100_000, "cancel-referral-funds")
    order = await shop.purchase(ALICE, product.id, "cancelled-referral")
    await shop.admin_order(ADMIN, order.id, "cancelled")
    assert (await shop.profile(BOB)).bonus_grosze == 0
    assert (await shop.referral_stats(BOB))["successful"] == 0


async def test_self_referral_is_ignored(shop):
    user = await shop.register(ALICE, referrer_telegram_id=ALICE)
    assert user.referrer_id is None
    assert (await shop.referral_stats(ALICE))["invited"] == 0


async def test_existing_user_cannot_change_referrer(shop):
    original = await shop.register(BOB)
    await shop.register(CHARLIE)
    await shop.register(ALICE, referrer_telegram_id=BOB)
    user = await shop.register(ALICE, referrer_telegram_id=CHARLIE)
    assert user.referrer_id == original.id


async def test_unknown_referrer_does_not_create_broken_relationship(shop):
    user = await shop.register(ALICE, referrer_telegram_id=999999)
    assert user.referrer_id is None


async def test_admin_access_uses_numeric_id(shop, buyer, product):
    with pytest.raises(DomainError):
        await shop.admin_balance(ALICE, ALICE, 1000, "unauthorized")
    with pytest.raises(DomainError):
        await shop.admin_orders(ALICE)
    with pytest.raises(DomainError):
        await shop.admin_product(ALICE, product.id, price_grosze=1)
    assert (await shop.profile(ALICE)).balance_grosze == 0


async def test_topup_approval_credits_exactly_once(shop, buyer):
    topup = await shop.create_topup(ALICE, 2500, "blik", "topup-key")
    duplicate = await shop.create_topup(ALICE, 2500, "blik", "topup-key")
    assert duplicate.id == topup.id
    assert (await shop.profile(ALICE)).balance_grosze == 0
    await asyncio.gather(
        shop.admin_topup(ADMIN, topup.id, True),
        shop.admin_topup(ADMIN, topup.id, True),
    )
    assert (await shop.profile(ALICE)).balance_grosze == 2500
    transactions = await shop.transactions(ALICE)
    assert len(transactions) == 1
    assert transactions[0].amount_grosze == 2500


async def test_rejected_topup_cannot_be_approved(shop, buyer):
    topup = await shop.create_topup(ALICE, 1000, "crypto", "reject-key")
    assert topup.external_id
    await shop.admin_topup(ADMIN, topup.id, False)
    with pytest.raises(DomainError):
        await shop.admin_topup(ADMIN, topup.id, True)
    assert (await shop.profile(ALICE)).balance_grosze == 0


async def test_topup_ownership_and_idempotency_binding(shop, buyer):
    topup = await shop.create_topup(ALICE, 1000, "crypto", "owner-key")
    await shop.register(BOB)
    with pytest.raises(DomainError):
        await shop.topup(BOB, topup.id)
    with pytest.raises(DomainError):
        await shop.create_topup(BOB, 1000, "crypto", "owner-key")
    with pytest.raises(DomainError):
        await shop.create_topup(ALICE, 2500, "crypto", "owner-key")


@pytest.mark.parametrize("amount,method", [(0, "blik"), (-100, "blik"), (1000, "real-bank")])
async def test_invalid_topup_rejected(shop, buyer, amount, method):
    with pytest.raises(DomainError):
        await shop.create_topup(ALICE, amount, method, "invalid-key")


async def test_balance_adjustment_idempotency_and_no_negative_balance(shop, buyer):
    await shop.admin_balance(ADMIN, ALICE, 2500, "balance-key")
    await shop.admin_balance(ADMIN, ALICE, 2500, "balance-key")
    assert (await shop.profile(ALICE)).balance_grosze == 2500
    with pytest.raises(DomainError):
        await shop.admin_balance(ADMIN, ALICE, -2501, "negative-balance")
    assert (await shop.profile(ALICE)).balance_grosze == 2500
    async with shop.db.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(AdminAuditLog)) >= 1


async def test_inactive_unavailable_and_blocked_purchase_rejected(shop, funded_buyer, product):
    await shop.admin_product(ADMIN, product.id, active=False)
    with pytest.raises(DomainError):
        await shop.purchase(ALICE, product.id, "inactive")
    await shop.admin_product(ADMIN, product.id, active=True, stock=0)
    with pytest.raises(DomainError):
        await shop.purchase(ALICE, product.id, "no-stock")
    await shop.admin_product(ADMIN, product.id, stock=5)
    await shop.admin_set_user_status(ADMIN, ALICE, "blocked")
    with pytest.raises(DomainError):
        await shop.purchase(ALICE, product.id, "blocked-account")
    assert (await shop.profile(ALICE)).balance_grosze == 100_000


async def test_product_with_order_cannot_be_deleted(shop, funded_buyer, product):
    await shop.purchase(ALICE, product.id, "history-must-survive")
    with pytest.raises(DomainError):
        await shop.admin_delete_product(ADMIN, product.id)
    assert (await shop.product(product.id)).name == product.name


async def test_unknown_order_and_product_are_domain_errors(shop, buyer):
    with pytest.raises(DomainError):
        await shop.purchase(ALICE, 999999, "missing-product")
    with pytest.raises(DomainError):
        await shop.admin_order(ADMIN, "missing-order", "completed")


async def test_purchase_retry_survives_database_engine_restart(shop, funded_buyer, product):
    order = await shop.purchase(ALICE, product.id, "persistent-operation")
    await shop.db.dispose()
    restarted_db = Database(shop.settings.database_url)
    restarted = ShopService(restarted_db, shop.settings)
    try:
        duplicate = await restarted.purchase(ALICE, product.id, "persistent-operation")
        assert duplicate.id == order.id
        assert (await restarted.profile(ALICE)).balance_grosze == 100_000 - product.price_grosze
        assert len(await restarted.orders(ALICE)) == 1
    finally:
        await restarted_db.dispose()


async def test_two_database_engines_serialize_last_item_purchase(shop, funded_buyer, product):
    await shop.register(BOB)
    await shop.admin_balance(ADMIN, BOB, 100_000, "second-buyer-funds")
    await shop.admin_product(ADMIN, product.id, stock=1)
    other_db = Database(shop.settings.database_url)
    other_shop = ShopService(other_db, shop.settings)
    try:
        results = await asyncio.gather(
            shop.purchase(ALICE, product.id, "first-engine-order"),
            other_shop.purchase(BOB, product.id, "second-engine-order"),
            return_exceptions=True,
        )
        assert sum(isinstance(result, Order) for result in results) == 1
        assert sum(isinstance(result, DomainError) for result in results) == 1
        assert (await shop.product(product.id)).stock == 0
        balances = [(await shop.profile(tg)).balance_grosze for tg in (ALICE, BOB)]
        assert sum(balances) == 200_000 - product.price_grosze
    finally:
        await other_db.dispose()


async def test_changed_price_requires_new_confirmation_and_preserves_snapshot(shop, funded_buyer, product):
    await shop.admin_product(ADMIN, product.id, price_grosze=product.price_grosze + 100)
    with pytest.raises(DomainError):
        await shop.purchase(
            ALICE, product.id, "stale-price-confirmation",
            expected_price_grosze=product.price_grosze,
        )
    assert (await shop.profile(ALICE)).balance_grosze == 100_000
    order = await shop.purchase(
        ALICE, product.id, "fresh-price-confirmation",
        expected_price_grosze=product.price_grosze + 100,
    )
    snapshot_name = order.product_name
    await shop.admin_product(ADMIN, product.id, name="Updated product DEMO", price_grosze=999)
    stored = (await shop.orders(ALICE))[0]
    assert stored.price_grosze == product.price_grosze + 100
    assert stored.product_name == snapshot_name


async def test_topup_approval_retry_survives_restart(shop, buyer):
    topup = await shop.create_topup(ALICE, 5000, "blik", "restart-topup-key")
    await shop.admin_topup(ADMIN, topup.id, True)
    await shop.db.dispose()
    restarted_db = Database(shop.settings.database_url)
    restarted = ShopService(restarted_db, shop.settings)
    try:
        again = await restarted.admin_topup(ADMIN, topup.id, True)
        assert again.status == "approved"
        assert (await restarted.profile(ALICE)).balance_grosze == 5000
        assert len(await restarted.transactions(ALICE)) == 1
    finally:
        await restarted_db.dispose()
