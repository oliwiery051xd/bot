"""User-facing automatic quotes exercise real routers without remote APIs."""
from decimal import Decimal

from test_auto_payments import auto_shop
from test_ui import feed, setup_ui


def user_screen(session):
    return next(call for call in reversed(session.calls)
                if getattr(call, 'chat_id', None) == 101
                and getattr(call, 'reply_markup', None) is not None)


def callback_button(screen, prefix):
    return next(button.callback_data for row in screen.reply_markup.inline_keyboard
                for button in row if (button.callback_data or '').startswith(prefix))


async def test_user_selects_fifty_pln_and_receives_automatic_eth_amount(auto_shop):
    service, cache, provider, clock = auto_shop
    dp, bot, session = setup_ui(service)
    try:
        await feed(dp, bot, '/start')
        await feed(dp, bot, 'crypto_asset:ETH', callback=True)
        assert 'automatycznie' in user_screen(session).text
        await feed(dp, bot, 'manual_amount:ETH:5000', callback=True)
        create = callback_button(user_screen(session), 'manual_create:')
        await feed(dp, bot, create, callback=True)
        quote = (await service.topups(101))[0]
        screen = user_screen(session)
        assert quote.status == 'awaiting_payment'
        assert Decimal(quote.crypto_amount) == Decimal('0.005')
        assert quote.wallet_address in screen.text
        assert quote.crypto_amount in screen.text
        assert 'Kurs:' in screen.text and 'Aktualizacja źródła:' in screen.text
        assert quote.rate_source in screen.text
        submit = callback_button(screen, 'topup_submit:')
        assert len(submit.encode()) <= 64
        await feed(dp, bot, submit, callback=True)
        assert (await service.topup(101, quote.id)).status == 'submitted'
        assert (await service.profile(101)).balance_grosze == 0
        assert provider.calls == 1
    finally:
        await dp.storage.close()
        await bot.session.close()


async def test_unavailable_rate_has_no_wallet_instructions_then_status_retries(auto_shop):
    service, cache, provider, clock = auto_shop
    provider.fail = True
    dp, bot, session = setup_ui(service)
    try:
        await feed(dp, bot, 'manual_amount:ETH:5000', callback=True)
        await feed(dp, bot, callback_button(user_screen(session), 'manual_create:'), callback=True)
        quote = (await service.topups(101))[0]
        screen = user_screen(session)
        assert quote.status == 'awaiting_quote'
        assert 'kurs jest chwilowo niedostępny' in screen.text
        assert quote.wallet_address not in screen.text
        assert not any((button.callback_data or '').startswith('topup_submit:')
                       for row in screen.reply_markup.inline_keyboard for button in row)
        provider.fail = False
        clock.advance(61)
        await feed(dp, bot, f'topup_status:{quote.id}', callback=True)
        assert quote.wallet_address in user_screen(session).text
        assert (await service.topup(101, quote.id)).status == 'awaiting_payment'
        assert (await service.profile(101)).balance_grosze == 0
    finally:
        await dp.storage.close()
        await bot.session.close()
