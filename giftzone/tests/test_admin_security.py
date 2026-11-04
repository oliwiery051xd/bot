"""Adversarial administrative updates through the real aiogram Dispatcher."""
from datetime import datetime, timezone
import time
from uuid import uuid4

import pytest
import pytest_asyncio
from aiogram.methods import AnswerCallbackQuery
from aiogram.types import CallbackQuery, Chat, Message, MessageOriginUser, Update, User
from sqlalchemy import func, select

from app.handlers.admin import AdminInput
from app.config import Settings
from app.database import Database
from app.models import AdminAuditLog
from app.services.shop import ShopService
from conftest import ADMIN, ALICE
from test_ui import button, feed, setup_ui


@pytest_asyncio.fixture
async def automatic_admin_shop(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path / 'automatic-admin.db'}"
    db = Database(url)
    await db.create_schema()
    settings = Settings(_env_file=None, database_url=url, demo_mode=False, admin_ids=[ADMIN],
                        crypto_auto_rates=True, crypto_eth_address="0x" + "1" * 40)
    service = ShopService(db, settings)
    await service.seed()
    await service.register(ALICE, "alice", "Alicja")
    yield service
    await db.dispose()


async def dispatch(dp, bot, text, *, actor_id=ADMIN, chat_type="private", chat_id=None,
                   callback=False, is_bot=False, missing_actor=False, forwarded=False,
                   sender_chat=False, business=False, inline=False, caption=False):
    actor = None if missing_actor else User(id=actor_id, is_bot=is_bot, first_name="Admin", username="monstal_owner")
    chat_id = chat_id if chat_id is not None else (actor_id if chat_type == "private" else -100123)
    message = Message(
        message_id=91, date=datetime.now(timezone.utc), chat=Chat(id=chat_id, type=chat_type),
        from_user=actor, text=None if caption else "MONSTAL" if callback else text,
        caption=text if caption else None,
        forward_origin=MessageOriginUser(date=datetime.now(timezone.utc), sender_user=actor) if forwarded else None,
        sender_chat=Chat(id=-100999, type="channel", title="Anonymous") if sender_chat else None,
        business_connection_id="business-test" if business else None,
    )
    if callback:
        update = Update(update_id=90, callback_query=CallbackQuery(
            id=uuid4().hex, from_user=actor, chat_instance="security-test",
            message=None if inline else message, inline_message_id="inline-test" if inline else None,
            data=text,
        ))
    else:
        update = Update(update_id=90, message=message)
    await dp.feed_update(bot, update)


@pytest.mark.parametrize("value,callback", [
    ("/admin", False), ("a:menu", True), ("a:users", True),
    ("a:balances", True), ("a:products:0", True), ("a:broadcast", True),
    ("a:stats", True), ("a:execute:" + "a" * 32, True),
])
async def test_allowlist_not_username_protects_admin_surfaces(shop, buyer, value, callback):
    dp, bot, session = setup_ui(shop)
    try:
        await dispatch(dp, bot, value, actor_id=ALICE, callback=callback)
        assert "Brak uprawnień" in session.calls[-1].text
        if callback:
            assert isinstance(session.calls[-1], AnswerCallbackQuery)
            assert session.calls[-1].show_alert is True
        assert (await shop.profile(ALICE)).balance_grosze == 0
        async with shop.db.sessions() as session_db:
            assert await session_db.scalar(select(func.count(AdminAuditLog.id))) == 0
    finally:
        await dp.storage.close()
        await bot.session.close()


@pytest.mark.parametrize("value,callback", [("/admin", False), ("a:stats", True), ("a:broadcast", True)])
@pytest.mark.parametrize("chat_type", ["group", "supergroup"])
async def test_allowlisted_admin_cannot_use_panel_in_groups(shop, value, callback, chat_type):
    dp, bot, session = setup_ui(shop)
    try:
        await dispatch(dp, bot, value, callback=callback, chat_type=chat_type)
        assert "prywatnej rozmowie" in session.calls[-1].text
        assert not any("STATYSTYKI" in (getattr(call, "text", "") or "") for call in session.calls)
    finally:
        await dp.storage.close()
        await bot.session.close()


@pytest.mark.parametrize("extra", [
    {"is_bot": True}, {"missing_actor": True}, {"sender_chat": True},
    {"forwarded": True}, {"business": True}, {"chat_id": 999999},
])
async def test_indirect_or_nonhuman_admin_messages_are_rejected(shop, extra):
    dp, bot, session = setup_ui(shop)
    try:
        await dispatch(dp, bot, "/admin", **extra)
        assert session.calls
        assert "ADMIN PANEL" not in (session.calls[-1].text or "")
    finally:
        await dp.storage.close()
        await bot.session.close()


@pytest.mark.parametrize("value,caption", [("  /admin", False), ("/admin", True), ("\n/admin", True)])
async def test_private_chat_policy_covers_command_whitespace_and_captions(shop, value, caption):
    dp, bot, session = setup_ui(shop)
    try:
        await dispatch(dp, bot, value, chat_type="supergroup", caption=caption)
        assert "prywatnej rozmowie" in session.calls[-1].text
        assert not any("ADMIN PANEL" in (getattr(call, "text", "") or "") for call in session.calls)
    finally:
        await dp.storage.close()
        await bot.session.close()


async def test_inline_admin_callbacks_are_rejected(shop):
    dp, bot, session = setup_ui(shop)
    try:
        await dispatch(dp, bot, "a:balances", callback=True, inline=True)
        assert isinstance(session.calls[-1], AnswerCallbackQuery)
        assert session.calls[-1].show_alert is True
        assert "prywatnej rozmowie" in session.calls[-1].text
    finally:
        await dp.storage.close()
        await bot.session.close()


async def test_injected_admin_fsm_cannot_grant_nonadmin_privileges(shop, buyer):
    dp, bot, session = setup_ui(shop)
    try:
        context = dp.fsm.get_context(bot=bot, chat_id=ALICE, user_id=ALICE)
        await context.set_state(AdminInput.confirmation)
        await context.set_data({"operation": "balance", "payload": {
            "target": ALICE, "delta": 50_000, "request_key": "forged-fsm-key",
        }, "token": "b" * 32, "expires_at": time.monotonic() + 600})
        await dispatch(dp, bot, "a:execute:" + "b" * 32, actor_id=ALICE, callback=True)
        assert "Brak uprawnień" in session.calls[-1].text
        assert await context.get_state() is None
        assert (await shop.profile(ALICE)).balance_grosze == 0
    finally:
        await dp.storage.close()
        await bot.session.close()


async def test_expired_admin_confirmation_never_mutates_balance(shop, buyer):
    dp, bot, session = setup_ui(shop)
    try:
        await feed(dp, bot, "/admin", user_id=ADMIN)
        await feed(dp, bot, "a:balances", user_id=ADMIN, callback=True)
        await feed(dp, bot, f"{ALICE} 25,00", user_id=ADMIN)
        execute = button(session, "a:execute:")
        context = dp.fsm.get_context(bot=bot, chat_id=ADMIN, user_id=ADMIN)
        await context.update_data(expires_at=time.monotonic() - 1)
        await feed(dp, bot, execute, user_id=ADMIN, callback=True)
        assert "wygasło" in session.calls[-1].text
        assert await context.get_state() is None
        assert (await shop.profile(ALICE)).balance_grosze == 0
    finally:
        await dp.storage.close()
        await bot.session.close()


async def test_group_execution_cannot_reuse_private_admin_confirmation(shop, buyer):
    dp, bot, session = setup_ui(shop)
    try:
        await feed(dp, bot, "/admin", user_id=ADMIN)
        await feed(dp, bot, "a:balances", user_id=ADMIN, callback=True)
        await feed(dp, bot, f"{ALICE} 25,00", user_id=ADMIN)
        execute = button(session, "a:execute:")
        await dispatch(dp, bot, execute, callback=True, chat_type="supergroup")
        assert "prywatnej rozmowie" in session.calls[-1].text
        assert (await shop.profile(ALICE)).balance_grosze == 0
        # The legitimate direct-chat confirmation still works once.
        await feed(dp, bot, execute, user_id=ADMIN, callback=True)
        assert (await shop.profile(ALICE)).balance_grosze == 2500
    finally:
        await dp.storage.close()
        await bot.session.close()


async def test_automatic_quotes_hide_manual_override_and_reject_forged_button(automatic_admin_shop):
    service = automatic_admin_shop
    # Build an unpriced persisted request without making external requests.
    service.settings.crypto_auto_rates = False
    request = await service.create_topup(ALICE, 5000, "crypto", "auto-admin-request", asset="ETH")
    service.settings.crypto_auto_rates = True
    dp, bot, session = setup_ui(service)
    try:
        await feed(dp, bot, f"a:topup:{request.id}", user_id=ADMIN, callback=True)
        markup = next(call.reply_markup for call in reversed(session.calls) if getattr(call, "reply_markup", None))
        assert not any((item.callback_data or "").startswith("a:tquote:") for row in markup.inline_keyboard for item in row)
        await feed(dp, bot, f"a:tquote:{request.id}", user_id=ADMIN, callback=True)
        assert "automatycznie" in session.calls[-1].text
        assert (await service.topup(ALICE, request.id)).crypto_amount is None
    finally:
        await dp.storage.close()
        await bot.session.close()


@pytest.mark.parametrize("state_name,value", [(AdminInput.value, "0.5"), (AdminInput.confirmation, "a:execute:" + "c" * 32)])
async def test_old_manual_quote_form_cannot_override_auto_mode(automatic_admin_shop, state_name, value):
    dp, bot, session = setup_ui(automatic_admin_shop)
    try:
        context = dp.fsm.get_context(bot=bot, chat_id=ADMIN, user_id=ADMIN)
        await context.set_state(state_name)
        await context.set_data({"operation": "topup_quote", "topup_id": "not-a-request",
                                "payload": {"topup_id": "not-a-request", "crypto_amount": "0.5"},
                                "token": "c" * 32, "expires_at": time.monotonic() + 600})
        await feed(dp, bot, value, user_id=ADMIN, callback=state_name == AdminInput.confirmation)
        assert "automatycznie" in session.calls[-1].text
        assert await context.get_state() is None
    finally:
        await dp.storage.close()
        await bot.session.close()


async def test_admin_settings_shows_auto_mode_without_exposing_api_key(automatic_admin_shop):
    automatic_admin_shop.settings.coingecko_api_key = "not-for-rendering-secret"
    dp, bot, session = setup_ui(automatic_admin_shop)
    try:
        await feed(dp, bot, "a:settings", user_id=ADMIN, callback=True)
        text = next(call.text for call in reversed(session.calls) if getattr(call, "reply_markup", None))
        assert "Wyceny krypto: automatyczne" in text
        assert "CoinGecko" in text
        assert "nie pobrano" in text
        assert "not-for-rendering-secret" not in text
    finally:
        await dp.storage.close()
        await bot.session.close()
