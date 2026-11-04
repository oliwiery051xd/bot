"""Exercise real aiogram routing against a local, deterministic Telegram transport."""
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.enums import ParseMode
from aiogram.methods import AnswerCallbackQuery
from aiogram.fsm.storage.memory import MemoryStorage, SimpleEventIsolation
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from app.handlers import create_router


class FakeTelegram(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls = []

    async def close(self):
        pass

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if method.__api_method__ == 'getMe':
            return User(id=123456, is_bot=True, first_name='MONSTAL', username='MonstalTestBot')
        if method.__api_method__ in ('sendMessage', 'sendPhoto'):
            return Message(message_id=len(self.calls), date=datetime.now(timezone.utc), chat=Chat(id=int(method.chat_id), type='private'), text=getattr(method, 'text', None))
        return True

    async def stream_content(self, url, headers=None, timeout=30, chunk_size=65536, raise_for_status=True):
        yield b''


async def feed(dp, bot, value, *, user_id=101, callback=False):
    user = User(id=user_id, is_bot=False, first_name='Alicja', username='alice')
    message = Message(message_id=42, date=datetime.now(timezone.utc), chat=Chat(id=user_id, type='private'), from_user=user, text=value if not callback else 'MONSTAL')
    if callback:
        update = Update(update_id=1, callback_query=CallbackQuery(id=uuid4().hex, from_user=user, chat_instance='demo', message=message, data=value))
    else:
        update = Update(update_id=1, message=message)
    await dp.feed_update(bot, update)


def setup_ui(shop):
    session = FakeTelegram()
    bot = Bot('123456:TEST_ONLY_FAKE_TOKEN', session=session, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage(), events_isolation=SimpleEventIsolation())
    dp.include_router(create_router(shop, shop.settings))
    return dp, bot, session


def rendered(session):
    return next(c for c in reversed(session.calls) if getattr(c, 'reply_markup', None) is not None)


def button(session, prefix):
    for row in rendered(session).reply_markup.inline_keyboard:
        for item in row:
            if item.callback_data and item.callback_data.startswith(prefix):
                return item.callback_data
    raise AssertionError(f'No button starting with {prefix!r}')


@pytest.mark.asyncio
async def test_start_profile_catalog_referrals_and_help(shop):
    dp, bot, session = setup_ui(shop)
    try:
        await feed(dp, bot, '/start')
        assert (await shop.profile(101)).username == 'alice'
        for value in ('profile', 'catalog', 'category:1', 'product:1', 'referrals', 'help', 'topups'):
            await feed(dp, bot, value, callback=True)
        texts = [getattr(c, 'text', '') or getattr(c, 'caption', '') or '' for c in session.calls]
        assert any('TWÓJ PROFIL' in t for t in texts)
        assert any('PROGRAM POLECEŃ' in t and 'ref_101' in t for t in texts)
        assert not any('Wystąpił błąd' in t for t in texts)
    finally:
        await dp.storage.close()
        await bot.session.close()


@pytest.mark.asyncio
async def test_unauthorized_callback_does_not_change_balance(shop):
    dp, bot, session = setup_ui(shop)
    try:
        await feed(dp, bot, 'a:menu', callback=True)
        assert any('Brak uprawnień' in (getattr(c, 'text', '') or '') for c in session.calls)
    finally:
        await dp.storage.close()


@pytest.mark.asyncio
async def test_cancel_admin_form_and_purchase_retry(shop, funded_buyer, product):
    dp, bot, session = setup_ui(shop)
    try:
        await feed(dp, bot, '/admin', user_id=9001)
        await feed(dp, bot, 'a:balances', user_id=9001, callback=True)
        await feed(dp, bot, '/cancel', user_id=9001)
        assert await dp.storage.get_state(dp.fsm.get_context(bot=bot, chat_id=9001, user_id=9001).key) is None
        key = uuid4().hex
        callback = f'purchase:{product.id}:{product.price_grosze}:{key}'
        await feed(dp, bot, callback, callback=True)
        await feed(dp, bot, callback, callback=True)
        assert len(await shop.orders(101)) == 1
        assert (await shop.profile(101)).balance_grosze == 100_000 - product.price_grosze
        texts = [getattr(c, 'text', '') or '' for c in session.calls]
        assert not any('Wystąpił błąd' in t for t in texts)
    finally:
        await dp.storage.close()


async def test_start_menu_two_columns(shop):
    dp, bot, session = setup_ui(shop)
    await feed(dp, bot, '/start')
    menu = rendered(session)
    assert 'Wersja: DEMO' in menu.text
    assert [len(row) for row in menu.reply_markup.inline_keyboard] == [2, 2, 2]
    assert button(session, 'catalog') == 'catalog'
    await dp.storage.close()


async def test_product_escapes_external_html(shop, product):
    dp, bot, session = setup_ui(shop)
    await shop.admin_product(9001, product.id, name='<b>Injected</b>', description='<script>no</script>')
    await feed(dp, bot, f'product:{product.id}', callback=True)
    text = rendered(session).text
    assert '&lt;b&gt;Injected&lt;/b&gt;' in text
    assert '&lt;script&gt;no&lt;/script&gt;' in text
    await dp.storage.close()


async def test_catalog_purchase_confirmation_and_repeat_click(shop, funded_buyer, product):
    dp, bot, session = setup_ui(shop)
    await feed(dp, bot, '/start')
    await feed(dp, bot, 'catalog', callback=True)
    await feed(dp, bot, button(session, 'category:'), callback=True)
    await feed(dp, bot, button(session, 'product:'), callback=True)
    await feed(dp, bot, button(session, 'purchase_confirm:'), callback=True)
    data = button(session, 'purchase:')
    assert 'Z testowego salda pobierzemy' in rendered(session).text
    assert not await shop.orders(101)
    await feed(dp, bot, data, callback=True)
    await feed(dp, bot, data, callback=True)
    orders = await shop.orders(101)
    assert len(orders) == 1
    assert orders[0].price_grosze == product.price_grosze
    assert (await shop.profile(101)).balance_grosze == 100_000 - product.price_grosze
    await dp.storage.close()


async def test_demo_topup_custom_amount_and_admin_approval(shop):
    dp, bot, session = setup_ui(shop)
    await feed(dp, bot, '/start')
    for data in ('topups', 'topup_method:crypto', 'topup_custom:crypto'):
        await feed(dp, bot, data, callback=True)
    await feed(dp, bot, '25,50')
    await feed(dp, bot, button(session, 'topup_create:'), callback=True)
    topup = (await shop.topups(101))[0]
    assert topup.amount_grosze == 2550
    assert topup.status == 'pending'
    assert (await shop.profile(101)).balance_grosze == 0
    await feed(dp, bot, f'topup_status:{topup.id}', callback=True)
    assert 'fikcyjna' in rendered(session).text
    assert 'FICTIONAL-DEMO-' in rendered(session).text
    await feed(dp, bot, '/admin', user_id=9001)
    await feed(dp, bot, f'a:topup:{topup.id}', user_id=9001, callback=True)
    await feed(dp, bot, button(session, 'a:tconf:'), user_id=9001, callback=True)
    assert (await shop.topup(101, topup.id)).status == 'pending'
    approve = button(session, 'a:trun:')
    await feed(dp, bot, approve, user_id=9001, callback=True)
    await feed(dp, bot, approve, user_id=9001, callback=True)
    assert (await shop.profile(101)).balance_grosze == 2550
    assert (await shop.topup(101, topup.id)).status == 'approved'
    await dp.storage.close()


async def test_unauthorized_admin_command_and_forged_callback(shop):
    dp, bot, session = setup_ui(shop)
    await feed(dp, bot, '/start')
    await feed(dp, bot, '/admin')
    assert 'Brak uprawnień' in session.calls[-1].text
    await feed(dp, bot, 'a:status:101:blocked', callback=True)
    assert isinstance(session.calls[-1], AnswerCallbackQuery)
    assert session.calls[-1].show_alert is True
    assert 'Brak uprawnień' in session.calls[-1].text
    assert (await shop.profile(101)).status == 'active'
    await dp.storage.close()


async def test_balance_form_requires_confirmation_and_rejects_replay(shop):
    dp, bot, session = setup_ui(shop)
    await feed(dp, bot, '/start')
    await feed(dp, bot, '/admin', user_id=9001)
    await feed(dp, bot, 'a:balances', user_id=9001, callback=True)
    await feed(dp, bot, '101 12,34', user_id=9001)
    assert (await shop.profile(101)).balance_grosze == 0
    execute = button(session, 'a:execute:')
    await feed(dp, bot, execute, user_id=9001, callback=True)
    assert (await shop.profile(101)).balance_grosze == 1234
    await feed(dp, bot, execute, user_id=9001, callback=True)
    assert 'wygasło' in session.calls[-1].text
    assert (await shop.profile(101)).balance_grosze == 1234
    await dp.storage.close()


@pytest.mark.parametrize('data', ['product:not-a-number', 'purchase:broken', 'not_a_known_button'])
async def test_malformed_user_callbacks_return_alert(shop, data):
    dp, bot, session = setup_ui(shop)
    await feed(dp, bot, data, callback=True)
    assert isinstance(session.calls[-1], AnswerCallbackQuery)
    assert session.calls[-1].show_alert is True
    await dp.storage.close()


async def test_other_users_cannot_open_topup_status(shop):
    dp, bot, session = setup_ui(shop)
    await feed(dp, bot, '/start')
    topup = await shop.create_topup(101, 1000, 'blik', 'ui-owner-key')
    await feed(dp, bot, f'topup_status:{topup.id}', user_id=102, callback=True)
    assert isinstance(session.calls[-1], AnswerCallbackQuery)
    assert 'nie istnieje' in session.calls[-1].text
    assert (await shop.topup(101, topup.id)).status == 'pending'
    await dp.storage.close()


async def test_admin_order_completion_sends_demo_code_and_refund_restores_balance(shop, funded_buyer, product):
    dp, bot, session = setup_ui(shop)
    order = await shop.purchase(101, product.id, 'ui-order-lifecycle')
    await feed(dp, bot, '/admin', user_id=9001)
    await feed(dp, bot, f'a:order:{order.id}', user_id=9001, callback=True)
    # Completion requires a separate confirmation screen before any DB mutation.
    await feed(dp, bot, f'a:oconf:{order.id}:completed', user_id=9001, callback=True)
    assert (await shop.orders(101))[0].status == 'pending'
    await feed(dp, bot, button(session, 'a:orun:'), user_id=9001, callback=True)
    completed = (await shop.orders(101))[0]
    assert completed.status == 'completed'
    assert completed.demo_code.startswith('DEMO-NOT-VALID-')
    notices = [method for method in session.calls if method.__api_method__ == 'sendMessage' and method.chat_id == 101]
    assert any(completed.demo_code in notice.text and 'nie można go zrealizować' in notice.text for notice in notices)
    await feed(dp, bot, f'a:oconf:{order.id}:refunded', user_id=9001, callback=True)
    await feed(dp, bot, button(session, 'a:orun:'), user_id=9001, callback=True)
    assert (await shop.orders(101))[0].status == 'refunded'
    assert (await shop.profile(101)).balance_grosze == 100_000
    await dp.storage.close()
